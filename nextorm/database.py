"""Database — binds a provider and owns a set of entities.

Usage::

    from nextorm import Database, Entity, Req


    class User(Entity):
        name: Req[str]
        email: Req[str]


    db = Database(entities=[User])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        User(name="alice", email="alice@example.com")  # auto-INSERTed at session end
    # to get PK immediately: flush() inside the session
"""

from __future__ import annotations

import contextlib
import time
import typing
from typing import TYPE_CHECKING, Any, cast

import nextorm.providers  # noqa: F401  # pyright: ignore[reportUnusedImport]
from nextorm.debug import QueryStat, _global_stats_lock, _print_sql, global_stats
from nextorm.entity import Entity, EntityMeta, _entity_registry
from nextorm.exceptions import MappingError, OptimisticCheckError
from nextorm.fields import (
    OptAttrValue,
)
from nextorm.pool import ConnectionPool
from nextorm.providers.base import (
    _PROVIDER_REGISTRY,
    SyncConnection,
    SyncProvider,
    get_sync_provider,
)
from nextorm.schema.builder import build_schema
from nextorm.schema.core import Table
from nextorm.schema.ddl import DDLRenderer, MariaDBRenderer, PostgresRenderer, SQLiteRenderer
from nextorm.schema.diff import diff_schemas
from nextorm.sql.builder import PARAM_STYLE_BUILDERS, SQLBuilder, SQLiteBuilder
from nextorm.sql.nodes import BinOp, ColumnRef, Delete, Insert, Literal, Param, Select, Update

if TYPE_CHECKING:
    from nextorm.query import QuerySet

__all__ = ["Database"]


# ---------------------------------------------------------------------------
# Global database registry
# ---------------------------------------------------------------------------
# All Database / AsyncDatabase instances that have called generate_mapping()
# are registered here so that Entity.__init__ can locate the db without
# requiring the db object to be a module-level variable.
_database_registry: list[Any] = []

_DDL_RENDERERS: dict[str, type[DDLRenderer]] = {
    "sqlite": SQLiteRenderer,
    "postgres": PostgresRenderer,
    "mariadb": MariaDBRenderer,
}


# ---------------------------------------------------------------------------
# Composite-PK helpers (shared by Database and the async counterpart)
# ---------------------------------------------------------------------------


def _pk_col_for(entity_cls: type[Entity], field_name: str) -> str:
    """Return the SQL column name for a PK field name.

    For a scalar field this is ``FieldSpec.column or field_name``.
    For a relation field it is ``<field_name>_id``.
    """
    if field_name in entity_cls._fields_:
        return entity_cls._fields_[field_name].spec.column or field_name
    # Relation field — column is <rel>_id
    return f"{field_name}_id"


def _pk_val_for(entity: Entity, field_name: str) -> Any:
    """Return the current PK column value for *field_name* on *entity*.

    Scalar fields are read via ``getattr``; relation fields read the stored
    FK id from ``entity.__dict__``.
    """
    entity_cls = type(entity)
    if field_name in entity_cls._fields_:
        return getattr(entity, field_name)
    return entity.__dict__.get(f"_{field_name}_id")


def _get_pk_val(entity: Entity) -> Any:
    """Return the primary-key value for *entity* (scalar or tuple).

    Returns ``None`` if the entity has no PK or if any PK part is ``None``
    (meaning the entity has not been saved yet).
    """
    pk_fields = type(entity)._pk_fields_
    if not pk_fields:
        return None
    vals = [_pk_val_for(entity, f) for f in pk_fields]
    if any(v is None for v in vals):
        return None
    return vals[0] if len(vals) == 1 else tuple(vals)


def _build_pk_where(entity_cls: type[Entity], pk_val: Any) -> BinOp:
    """Build a SQL WHERE clause for a single or composite primary key.

    *pk_val* must be the scalar value (single PK) or a tuple of values
    (composite PK) in the same order as ``entity_cls._pk_fields_``.
    """
    pk_fields = entity_cls._pk_fields_
    if len(pk_fields) == 1:
        col = _pk_col_for(entity_cls, pk_fields[0])
        return BinOp(ColumnRef(col), "=", Param(value=pk_val))
    # Composite: AND-chain all parts
    vals: tuple[Any, ...] = pk_val  # must be a tuple
    node: BinOp = BinOp(ColumnRef(_pk_col_for(entity_cls, pk_fields[0])), "=", Param(value=vals[0]))
    for fname, fval in zip(pk_fields[1:], vals[1:], strict=False):
        node = BinOp(
            node,
            "AND",
            BinOp(ColumnRef(_pk_col_for(entity_cls, fname)), "=", Param(value=fval)),
        )
    return node


class Database:
    """Owns a provider binding, a persistent connection, and entity schema.

    The database keeps a single open connection so that in-memory SQLite
    databases (``":memory:"``) remain accessible across multiple operations.

    Call :meth:`close` (or use the database as a context manager) to release
    the connection when done.

    Parameters
    ----------
    entities:
        Explicit list of entity classes to include.  When omitted, *all*
        :class:`~nextorm.entity.Entity` subclasses registered via
        :data:`~nextorm.entity._entity_registry` are used.
    """

    #: Distinguishes this class from :class:`~nextorm.async_database.AsyncDatabase`
    #: without requiring a circular import.  Checked by ``FieldDescriptor.__get__`` to
    #: decide whether a lazy-field load can be executed synchronously.
    _is_async: bool = False

    def __init__(self, entities: list[type[Entity]] | None = None) -> None:
        self._entities: list[type[Entity]] | None = list(entities) if entities is not None else None
        self._provider: str | None = None
        self._connect_args: tuple[Any, ...] = ()
        self._connect_kwargs: dict[str, Any] = {}
        self._renderer: DDLRenderer | None = None
        self._sync_provider_instance: SyncProvider | None = None
        self._builder: SQLBuilder | None = None
        self._connection: SyncConnection | None = None
        self._pool: ConnectionPool | None = None
        self._schema: dict[str, Table] = {}
        self._bound: bool = False
        self._last_sql: str = ""
        self._local_stats: dict[str, QueryStat] = {}

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Entity registration
    # ------------------------------------------------------------------

    def register(self, *entity_classes: type[Entity]) -> None:
        """Add one or more entity classes to this database's scope.

        Entities register themselves globally at import time; call this method
        to opt specific classes into a database that was constructed with an
        explicit ``entities=[...]`` list, or to add entities after construction.

        Calling ``register()`` with an entity that is already registered is a
        no-op.  Each argument must be an :class:`~nextorm.entity.Entity`
        subclass — passing anything else raises :exc:`TypeError`.

        Example::

            db = Database(entities=[User])
            db.register(Post, Comment)  # add more after the fact
            db.bind("sqlite", ":memory:")
            db.generate_mapping(create_tables=True)
        """
        for cls in entity_classes:
            if not isinstance(cls, EntityMeta):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise TypeError(f"{cls!r} is not an Entity subclass")
            if self._entities is None:
                self._entities = []
            if cls not in self._entities:
                self._entities.append(cls)

    @property
    def entities(self) -> dict[str, type[Entity]]:
        """Return ``{entity_name: entity_class}`` for all entities in this DB."""
        return {cls.__name__: cast("type[Entity]", cls) for cls in self._effective_entities()}

    def _effective_entities(self) -> list[EntityMeta]:
        """Return the entity list, falling back to the global registry."""
        if self._entities is not None:
            return list(self._entities)
        return sorted(
            (cls for cls in _entity_registry),
            key=lambda c: c.__name__,
        )

    # ------------------------------------------------------------------
    # Provider binding
    # ------------------------------------------------------------------

    def bind(
        self,
        provider: str,
        *args: Any,
        pool_min: int = 0,
        pool_max: int = 1,
        pool_timeout: float = 30.0,
        **kwargs: Any,
    ) -> None:
        """Bind the database to a named provider.

        Parameters
        ----------
        provider:
            Registered provider name (``"sqlite"``, ``"postgres"``, ``"mariadb"``).
        \\*args / \\*\\*kwargs:
            Connection arguments forwarded to the provider (e.g. the database
            path for SQLite, the DSN for PostgreSQL).
        pool_min:
            Minimum (pre-created) pool connections.  ``0`` means no connections
            are opened until the first query.
        pool_max:
            Maximum pool size.  ``1`` (the default) keeps the legacy
            single-persistent-connection behaviour.  Values ``> 1`` enable
            true pooling — each operation checks out a connection from the pool
            and returns it afterwards.
        pool_timeout:
            Seconds to wait for a free connection before
            :exc:`~nextorm.pool.PoolTimeoutError` is raised.
        """
        if provider not in _PROVIDER_REGISTRY:
            raise ValueError(
                f"Unknown provider {provider!r}. Available: {sorted(_PROVIDER_REGISTRY)}"
            )
        self._provider = provider
        self._connect_args = args
        self._connect_kwargs = kwargs
        if provider in _DDL_RENDERERS:
            self._renderer = _DDL_RENDERERS[provider]()
        self._sync_provider_instance = get_sync_provider(provider)()
        builder_cls = PARAM_STYLE_BUILDERS.get(
            self._sync_provider_instance.param_style, SQLiteBuilder
        )
        self._builder = builder_cls()
        self._bound = True
        if pool_max > 1 or pool_min > 0:
            provider_inst = self._sync_provider_instance

            def _factory() -> SyncConnection:
                return provider_inst.connect(*args, **kwargs)

            self._pool = ConnectionPool(
                _factory, min_size=pool_min, max_size=pool_max, timeout=pool_timeout
            )

    def unbind(self) -> None:
        """Close the connection and clear the provider binding."""
        self.close()  # close() already deregisters from _database_registry
        self._provider = None
        self._connect_args = ()
        self._connect_kwargs = {}
        self._renderer = None
        self._sync_provider_instance = None
        self._builder = None
        self._pool = None
        self._schema = {}
        self._bound = False

    @property
    def is_bound(self) -> bool:
        """Return ``True`` when :meth:`bind` has been called successfully."""
        return self._bound

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _ensure_connection(self) -> SyncConnection:
        """Return the open connection, opening one if necessary.

        When a :class:`~nextorm.pool.ConnectionPool` is configured, returns
        a *checked-out* connection from the pool.  The caller is responsible
        for releasing it via :meth:`_release_connection`.
        """
        if not self._bound:
            raise RuntimeError("Database is not bound to a provider. Call bind() first.")
        if self._pool is not None:
            return cast("SyncConnection", self._pool.acquire())
        assert self._sync_provider_instance is not None
        if self._connection is None:
            self._connection = self._sync_provider_instance.connect(
                *self._connect_args, **self._connect_kwargs
            )
        return self._connection

    def _release_connection(self, conn: SyncConnection) -> None:
        """Return *conn* to the pool (no-op when pool is not configured)."""
        if self._pool is not None:
            self._pool.release(conn)

    def close(self) -> None:
        """Close the persistent connection (if open) or all pool connections."""
        if self._pool is not None:
            self._pool.close_all()
        elif self._connection is not None:
            self._connection.close()
            self._connection = None
        with contextlib.suppress(ValueError):
            _database_registry.remove(self)

    def commit(self) -> None:
        """Flush pending changes then commit the current database transaction.

        Calls :meth:`flush` first so all pending inserts/updates are written,
        then finalises the underlying connection's transaction.  This mirrors
        PonyORM's approach where ``commit()`` integrates the flush step
        internally — callers never need to flush manually before committing.
        """
        self.flush()
        self._commit_transaction()

    def _commit_transaction(self) -> None:
        """Issue SQL COMMIT without an implicit flush.

        Used by the staged-commit logic in the module-level
        :func:`~nextorm.commit` and the session-exit path so that flush and
        commit can be separated (flush all first, then commit primary, then
        commit secondaries).
        """
        conn = self._ensure_connection()
        try:
            conn.commit()
        finally:
            self._release_connection(conn)

    def _rollback_transaction(self) -> None:
        """Issue SQL ROLLBACK without clearing the session cache.

        Unlike :meth:`rollback` (which also clears the session identity map),
        this low-level variant is used during partial-commit cleanup where the
        session cache will be cleared separately after all databases are processed.
        """
        conn = self._ensure_connection()
        try:
            conn.rollback()
        finally:
            self._release_connection(conn)

    def rollback(self) -> None:
        """Roll back the current database transaction and clear the session cache.

        Discards uncommitted work on the active connection and wipes the
        identity map of the current :func:`~nextorm.session.db_session`.
        When called outside a session the cache-clear step is a no-op.
        """
        self._rollback_transaction()
        # Also clear the session identity map so stale objects aren't reused.
        from nextorm.session import _get_session_stack  # noqa: PLC0415

        cache = _get_session_stack().current
        if cache is not None:
            cache.clear()

    def flush(self) -> None:
        """Write all pending dirty and new objects in the current session to the DB.

        Iterates the current :func:`~nextorm.session.db_session` cache and
        calls :meth:`save` on every object that has been scheduled for INSERT
        or marked dirty that belongs to *this* database.  Because NextORM
        auto-commits per DML statement, each ``save`` is independently committed.

        When called outside a session this is a no-op.
        """
        from nextorm.session import _get_session_stack  # noqa: PLC0415

        cache = _get_session_stack().current
        if cache is None:
            return
        for entity in list(cache.objects_to_save):
            entity_db = vars(entity).get("_db_")
            # Save if this is the right DB, or if the entity has no DB yet
            # (unmapped entity — _require_mapped will raise the appropriate error).
            if entity_db is None or entity_db is self:
                self.save(entity)
        for entity in list(cache.dirty_objects):
            entity_db = vars(entity).get("_db_")
            if entity_db is None or entity_db is self:
                self.save(entity)

    # ------------------------------------------------------------------
    # Schema mapping
    # ------------------------------------------------------------------

    def generate_mapping(
        self, *, create_tables: bool = False, validate_relations: bool = True
    ) -> None:
        """Build the internal schema and optionally create database tables.

        Must be called after :meth:`bind`.  Registers this database instance
        so that :func:`~nextorm.entity._find_db_for_entity` can locate it.

        Parameters
        ----------
        create_tables:
            When ``True``, execute ``CREATE TABLE`` DDL for every entity in
            the schema.  The connection is kept open so that in-memory SQLite
            databases (``":memory:"``) remain accessible after the call.
        validate_relations:
            When ``True``, every declared ``Set[T]`` relation is checked to
            have a matching ``Single[T]`` back-reference on the target entity.
            Ambiguous cases (multiple candidates with no ``reverse=``) raise
            :exc:`~nextorm.exceptions.MappingError`.

        Example::

            db = Database(entities=[User, Post])
            db.bind("sqlite", ":memory:")
            db.generate_mapping(create_tables=True)
        """
        if not self._bound:
            raise RuntimeError("Database is not bound to a provider. Call bind() first.")

        if validate_relations:
            self._validate_relations()

        self._schema = build_schema(cast("list[type[Entity]]", self._effective_entities()))

        # Register so Entity.__init__ can auto-locate this database.
        if self not in _database_registry:
            _database_registry.append(self)

        if create_tables:
            assert self._renderer is not None
            self._ddl_statements = [
                self._renderer.create_table(table) for table in self._schema.values()
            ]
            assert self._sync_provider_instance is not None
            conn = self._ensure_connection()
            try:
                self._sync_provider_instance.execute_ddl(conn, self._ddl_statements)
            finally:
                self._release_connection(conn)

    @property
    def schema(self) -> dict[str, Table]:
        """Current schema mapping (empty until :meth:`generate_mapping` is called)."""
        return dict(self._schema)

    def migrate(self) -> list[str]:
        """Apply pending schema changes to the live database.

        Introspects the current database schema, computes the diff against
        the entity-derived target schema (built by :meth:`generate_mapping`),
        and executes each pending DDL operation (``CREATE TABLE``,
        ``ALTER TABLE … ADD/DROP COLUMN``, ``CREATE/DROP INDEX``, etc.).

        Returns the list of SQL statements that were executed.  An empty list
        means the database is already up to date.

        Raises :exc:`RuntimeError` if the database is not bound to a provider
        or if :meth:`generate_mapping` has not been called yet.
        """
        if not self._bound:
            raise RuntimeError("Database is not bound to a provider. Call bind() first.")
        if not self._schema:
            raise RuntimeError("Schema is empty. Call generate_mapping() before migrate().")
        assert self._sync_provider_instance is not None
        assert self._renderer is not None
        conn = self._ensure_connection()
        try:
            current = self._sync_provider_instance.introspect(conn)
            ops = diff_schemas(current, self._schema)
            stmts = [self._renderer.render(op) for op in ops]
            if stmts:
                self._sync_provider_instance.execute_ddl(conn, stmts)
        finally:
            self._release_connection(conn)
        return stmts

    def get_ddl(self) -> list[str]:
        """DDL statements from the last :meth:`generate_mapping` call."""
        return list(getattr(self, "_ddl_statements", []))

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def select[ET: Entity](self, entity_class: type[ET]) -> QuerySet[ET]:
        """Return a :class:`~nextorm.query.QuerySet` for *entity_class*.

        :meth:`generate_mapping` must have been called first.
        """
        if not self._schema:
            raise RuntimeError("Schema is empty. Call generate_mapping() before select().")
        table_name = entity_class._table_name_
        table = self._schema.get(table_name)
        if table is None:
            raise RuntimeError(
                f"Entity {entity_class.__name__!r} is not in the mapped schema. "
                "Ensure it was included when bind() / generate_mapping() was called."
            )
        assert self._builder is not None
        from nextorm.query import QuerySet as _QuerySet  # noqa: PLC0415
        from nextorm.sql.nodes import BinOp, ColumnRef, Param  # noqa: PLC0415

        qs: QuerySet[ET] = _QuerySet(entity_class, table, self, self._builder)
        # STI: automatically filter by discriminator value for child entities
        disc_val = entity_class._discriminator_val_
        if disc_val is not None and entity_class._sti_parent_ is not None:
            disc_col = entity_class._sti_parent_._discriminator_col_
            if disc_col is not None:  # pragma: no branch
                qs = qs.filter(BinOp(ColumnRef(disc_col), "=", Param(value=disc_val)))
        return qs

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, entity: Entity) -> None:
        """Insert or update *entity* in the database.

        * All PK columns are ``None`` → INSERT (auto-PK written back when single auto PK).
        * At least one PK is set → UPDATE all non-PK columns.

        Lifecycle hooks are called around each operation.  When a
        :func:`~nextorm.session.db_session` is active the entity is
        registered in its identity map after a successful save.
        """
        entity_cls = type(entity)
        self._require_mapped(entity_cls)
        table = self._schema[entity_cls._table_name_]
        pk_val = _get_pk_val(entity)

        # For composite PKs the user supplies all PK values before the first save.
        # Use _dbvals_ presence to distinguish new (not-yet-inserted) from existing.
        # We cannot use _db_ because Entity.__init__ sets _db_ immediately when
        # created inside a db_session (before the first actual DB write).
        is_new = pk_val is None or (entity_cls._pk_field_ is None and "_dbvals_" not in vars(entity))
        try:
            if is_new:
                entity.before_insert()
                self._do_insert(entity, entity_cls, table)
                entity.after_insert()
            else:
                entity.before_update()
                self._do_update(entity, entity_cls, table, pk_val)
                entity.after_update()
        except Exception:
            # Save failed — remove entity from session tracking so flush() won't retry.
            from nextorm.session import _get_session_stack  # noqa: PLC0415

            cache = _get_session_stack().current
            if cache is not None:
                cache.unmark_dirty(entity)
                cache.unschedule_save(entity)
            raise

        self._post_save(entity, pk_val if pk_val is not None else _get_pk_val(entity))

    def insert(self, entity: Entity) -> None:
        """Always INSERT *entity*, even when its primary key is already set.

        Unlike :meth:`save`, which uses the PK value to choose between INSERT
        and UPDATE, this method always performs an INSERT.  Use it for:

        * Entities with user-assigned (non-auto) primary keys.
        * Data loading / migration where you know the row does not yet exist.
        * Inserting a copy of an entity with a freshly assigned identity.

        When the PK is auto-generated (``FieldSpec(primary_key=True, auto=True)``):

        * PK is ``None`` → the database generates one, which is written back.
        * PK is a concrete value → that value is passed in the INSERT,
          overriding the auto-increment counter (useful for data migration).

        Lifecycle hooks ``before_insert`` / ``after_insert`` are called.
        """
        entity_cls = type(entity)
        self._require_mapped(entity_cls)
        table = self._schema[entity_cls._table_name_]
        entity.before_insert()
        self._do_insert(entity, entity_cls, table, include_auto_pk=True)
        entity.after_insert()
        self._post_save(entity, _get_pk_val(entity))

    # ------------------------------------------------------------------
    # Session / identity-map bookkeeping (shared by save + insert)
    # ------------------------------------------------------------------

    def _post_save(self, entity: Entity, pk_val: Any) -> None:
        """Register *entity* in the active session's identity map after a write."""
        from nextorm.session import _get_session_stack  # noqa: PLC0415

        cache = _get_session_stack().current
        if cache is not None and pk_val is not None:
            cache.put(entity, pk_val)
            cache.unmark_dirty(entity)
            cache.unschedule_save(entity)  # remove from INSERT queue after successful save

    def delete_instance(self, entity: Entity) -> None:
        """Delete *entity* from the database and clear its primary key.

        Raises :exc:`ValueError` when the entity has not been saved (PK is ``None``).
        """
        entity_cls = type(entity)
        self._require_mapped(entity_cls)
        pk_val = _get_pk_val(entity)
        pk_fields = entity_cls._pk_fields_
        if not pk_fields:
            raise RuntimeError(f"Entity {entity_cls.__name__!r} has no primary-key field.")
        if pk_val is None:
            raise ValueError(
                f"Cannot delete {entity_cls.__name__!r}: primary key is None "
                "(entity has not been saved)."
            )
        table = self._schema[entity_cls._table_name_]
        stmt = Delete(
            table=table.name,
            where=_build_pk_where(entity_cls, pk_val),
        )
        assert self._builder is not None
        sql, params = self._builder.render(stmt)
        entity.before_delete()
        self._execute_dml(sql, params)
        # Clear all PK fields
        for fname in pk_fields:
            if fname in entity_cls._fields_:
                setattr(entity, fname, None)
            else:
                vars(entity).pop(f"_{fname}_id", None)
        entity.after_delete()

        # Remove from the active session's identity map
        from nextorm.session import _get_session_stack  # noqa: PLC0415

        cache = _get_session_stack().current
        if cache is not None:
            cache.remove(entity, pk_val)

    # ------------------------------------------------------------------
    # Low-level SQL execution (used by QuerySet + save/delete)
    # ------------------------------------------------------------------

    def get_connection(self) -> Any:
        """Return the raw underlying DBAPI connection.

        Use this as an escape hatch when you need provider-specific features
        not covered by the NextORM API.  The returned type depends on the
        active provider (e.g. ``sqlite3.Connection`` for SQLite).

        Raises :exc:`RuntimeError` when the database is not connected.
        """
        conn = self._ensure_connection()
        # Unwrap the thin SyncConnection wrapper to reach the DBAPI connection.
        raw = getattr(conn, "_conn", conn)
        return raw

    def execute(self, sql: str, *args: Any) -> int:
        """Execute arbitrary SQL and return the number of affected rows.

        *args* are passed as positional parameters to the DBAPI cursor (i.e.
        bound as ``?`` or ``%s`` placeholders depending on the provider).

        This is useful for DDL statements or DML that cannot be expressed via
        the QuerySet API::

            db.execute("CREATE INDEX idx_user_email ON user (email)")
            db.execute("DELETE FROM session WHERE expires_at < ?", cutoff)
        """
        return self._execute_dml(sql, list(args))

    def select_raw(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """Execute a raw SELECT and return the results as a list of dicts.

        Column names are taken from the cursor description.  Use this when
        you need a join result or aggregate that doesn't map to a single
        entity class::

            rows = db.select_raw(
                "SELECT u.name, COUNT(o.id) AS order_count "
                'FROM user u LEFT JOIN "order" o ON o.user_id = u.id '
                "GROUP BY u.id"
            )
            for row in rows:
                print(row["name"], row["order_count"])
        """
        conn = self._ensure_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, list(args))
            desc = cur.description
            if desc is None:  # pragma: no cover
                return []
            col_names = [d[0] for d in desc]
            return [dict(zip(col_names, row, strict=False)) for row in cur.fetchall()]
        finally:
            self._release_connection(conn)

    def _execute(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        """Execute *sql* with *params* and return all rows."""
        self._last_sql = sql
        _print_sql(sql, params)
        conn = self._ensure_connection()
        try:
            cur = conn.cursor()
            t0 = time.perf_counter()
            cur.execute(sql, params)
            rows = list(cur.fetchall())
            self._record_stat(sql, time.perf_counter() - t0)
            return rows
        finally:
            self._release_connection(conn)

    def _execute_described(
        self, sql: str, params: list[Any]
    ) -> tuple[list[tuple[Any, ...]], list[str]]:
        """Execute *sql* and return ``(rows, column_names)``."""
        self._last_sql = sql
        _print_sql(sql, params)
        conn = self._ensure_connection()
        try:
            cur = conn.cursor()
            t0 = time.perf_counter()
            cur.execute(sql, params)
            rows = list(cur.fetchall())
            self._record_stat(sql, time.perf_counter() - t0)
            col_names = [d[0] for d in cur.description] if cur.description else []
            return rows, col_names
        finally:
            self._release_connection(conn)

    def _execute_dml(self, sql: str, params: list[Any]) -> int:
        """Execute a DML statement and return the affected row count."""
        self._last_sql = sql
        _print_sql(sql, params)
        conn = self._ensure_connection()
        try:
            cur = conn.cursor()
            t0 = time.perf_counter()
            cur.execute(sql, params)
            count = cur.rowcount
            # Auto-commit only outside a db_session; inside a session the commit
            # happens at session end (PonyORM semantics).
            from nextorm.session import _get_session_stack  # noqa: PLC0415

            if _get_session_stack().current is None:
                conn.commit()
            self._record_stat(sql, time.perf_counter() - t0)
            return count
        finally:
            self._release_connection(conn)

    def _execute_insert(self, sql: str, params: list[Any]) -> int | None:
        """Execute an INSERT and return the auto-generated row-id."""
        self._last_sql = sql
        _print_sql(sql, params)
        conn = self._ensure_connection()
        try:
            cur = conn.cursor()
            t0 = time.perf_counter()
            cur.execute(sql, params)
            rowid = cur.lastrowid
            # Auto-commit only outside a db_session.
            from nextorm.session import _get_session_stack  # noqa: PLC0415

            if _get_session_stack().current is None:
                conn.commit()
            self._record_stat(sql, time.perf_counter() - t0)
            return rowid
        finally:
            self._release_connection(conn)

    @property
    def last_sql(self) -> str:
        """The last SQL string sent to the database.

        Updated after every ``_execute``, ``_execute_dml``, and
        ``_execute_insert`` call.  Empty string if no query has been
        executed yet.  Useful for debugging::

            db.select(User).filter(User.age > 18).fetch_all()
            print(db.last_sql)
        """
        return self._last_sql

    # ------------------------------------------------------------------
    # Query statistics
    # ------------------------------------------------------------------

    @property
    def local_stats(self) -> dict[str, QueryStat]:
        """Per-database query statistics since the last :meth:`clear_local_stats` call.

        Returns a snapshot copy keyed by SQL string.  Each value is a
        :class:`~nextorm.debug.QueryStat` with ``count``, ``sum_time``,
        ``min_time``, ``max_time``, and ``avg_time`` attributes.
        """
        return dict(self._local_stats)

    def clear_local_stats(self) -> None:
        """Reset per-database query statistics."""
        self._local_stats.clear()

    def merge_local_stats(self) -> None:
        """Merge per-database stats into the module-level :data:`~nextorm.debug.global_stats`.

        Call this periodically (e.g. at the end of a request) to accumulate
        statistics across multiple :class:`Database` instances.
        """
        with _global_stats_lock:
            for sql, stat in self._local_stats.items():
                if sql not in global_stats:
                    global_stats[sql] = QueryStat()
                global_stats[sql]._merge(stat)

    def _record_stat(self, sql: str, elapsed: float) -> None:
        """Record one query execution in :attr:`_local_stats`."""
        if sql not in self._local_stats:
            self._local_stats[sql] = QueryStat()
        self._local_stats[sql]._record(elapsed)

    def _load_lazy_field(self, entity: Entity, field_name: str) -> OptAttrValue:
        """Execute a per-field SELECT to load a single lazy field value.

        Called automatically by :class:`~nextorm.entity.FieldDescriptor` when
        a lazy field is accessed on an entity that was loaded by this database.
        """
        entity_cls = type(entity)
        fi = entity_cls._fields_[field_name]
        col_name = fi.spec.column or field_name
        pk_val = _get_pk_val(entity)
        if pk_val is None:
            return None
        where = _build_pk_where(entity_cls, pk_val)
        stmt = Select(
            columns=(ColumnRef(col_name),),
            from_table=entity_cls._table_name_,
            where=where,
        )
        assert self._builder is not None
        sql, params = self._builder.render(stmt)
        rows = self._execute(sql, params)
        return rows[0][0] if rows else None

    # ------------------------------------------------------------------
    # INSERT / UPDATE helpers
    # ------------------------------------------------------------------

    def _do_insert(
        self,
        entity: Entity,
        entity_cls: type[Entity],
        table: Table,
        *,
        include_auto_pk: bool = False,
    ) -> None:
        # Determine the single auto-PK field name (None for composite PKs)
        single_pk_field = entity_cls._pk_field_  # None when composite
        # Auto-generate UUID / ULID PK values before the column list is built.
        for fi in entity_cls._fields_.values():
            if fi.spec.uuid_auto is not None and getattr(entity, fi.name) is None:
                import uuid as _uuid  # noqa: PLC0415

                if fi.spec.uuid_auto == "v7":
                    setattr(entity, fi.name, _generate_uuid7())
                elif fi.spec.uuid_auto == "v4":
                    setattr(entity, fi.name, _uuid.uuid4())
                else:  # "ulid"
                    setattr(entity, fi.name, _generate_ulid())
        # Exclude auto-PK from column list unless include_auto_pk=True AND it has a value.
        cols_and_vals: list[tuple[str, Any]] = [
            (fi.spec.column or fi.name, _serialize_value(getattr(entity, fi.name)))
            for fi in entity_cls._fields_.values()
            if not (
                fi.spec.primary_key
                and fi.spec.auto
                and (not include_auto_pk or getattr(entity, fi.name) is None)
            )
        ]
        # Also persist FK values from Single relations (owning side)
        for ri in entity_cls._relations_.values():
            if ri.spec.kind == RelationKind.SINGLE:
                fk_col = ri.spec.column or f"{ri.name}_id"
                fk_val = entity.__dict__.get(f"_{ri.name}_id")
                cols_and_vals.append((fk_col, fk_val))
        # STI: inject the discriminator column value for child entities
        disc_val = entity_cls._discriminator_val_
        if disc_val is not None and entity_cls._sti_parent_ is not None:
            disc_col = entity_cls._sti_parent_._discriminator_col_
            if disc_col is not None:  # pragma: no branch
                cols_and_vals.append((disc_col, disc_val))
        assert self._builder is not None
        if cols_and_vals:
            stmt = Insert(
                table=table.name,
                columns=tuple(c for c, _ in cols_and_vals),
                values=tuple(Param(value=v) for _, v in cols_and_vals),
            )
            sql, params = self._builder.render(stmt)
        else:
            # Entity has only an auto-pk — syntax varies by backend:
            #   sqlite:   INSERT INTO t VALUES (NULL)
            #   mariadb:  INSERT INTO t VALUES ()
            #   postgres: INSERT INTO t DEFAULT VALUES  (standard SQL)
            if self._provider == "sqlite":
                sql, params = f"INSERT INTO {table.name} VALUES (NULL)", []
            elif self._provider == "mariadb":
                sql, params = f"INSERT INTO {table.name} VALUES ()", []
            else:  # pragma: no cover — PostgreSQL DEFAULT VALUES; no live PG in test suite
                sql, params = f"INSERT INTO {table.name} DEFAULT VALUES", []
        rowid = self._execute_insert(sql, params)
        # Write the auto-generated PK back only for DB-side auto-increment PKs.
        # UUID / ULID PKs (spec.uuid_auto set, spec.auto=False) are already written
        # above before the INSERT — do not overwrite them with lastrowid.
        if single_pk_field is not None and rowid is not None:
            pk_fi = entity_cls._fields_.get(single_pk_field)
            if pk_fi is not None and pk_fi.spec.auto:
                setattr(entity, single_pk_field, rowid)
        # Attach database context so lazy-load works on newly saved entities
        vars(entity)["_db_"] = self
        # Set _dbvals_ so that subsequent attribute mutations are auto-dirtied.
        dbvals: dict[str, Any] = {}
        for fi in entity_cls._fields_.values():
            if not fi.spec.primary_key and not fi.spec.volatile:
                col = fi.spec.column or fi.name
                dbvals[col] = getattr(entity, fi.name, None)
        for ri in entity_cls._relations_.values():
            if ri.spec.kind == RelationKind.SINGLE:
                dbvals[ri.spec.column or f"{ri.name}_id"] = vars(entity).get(f"_{ri.name}_id")
        vars(entity)["_dbvals_"] = dbvals
        if "_read_cols_" not in vars(entity):
            vars(entity)["_read_cols_"] = set()

    def _do_update(
        self,
        entity: Entity,
        entity_cls: type[Entity],
        table: Table,
        pk_val: Any,
    ) -> None:
        # Snapshot _read_cols_ immediately — the assignments listcomp below calls
        # getattr on every field descriptor, which would otherwise pollute
        # _read_cols_ with all SET-clause columns before we inspect it.
        read_cols_snapshot: frozenset[str] = frozenset(vars(entity).get("_read_cols_") or ())
        dbvals: dict[str, Any] | None = vars(entity).get("_dbvals_")

        # For composite PKs, scalar PK fields are not marked fi.spec.primary_key.
        # Exclude them explicitly so the UPDATE SET clause only touches non-PK columns.
        scalar_pk_names: frozenset[str] = frozenset(
            f for f in entity_cls._pk_fields_ if f in entity_cls._fields_
        )
        assignments: list[tuple[str, Param]] = [
            (fi.spec.column or fi.name, Param(value=_serialize_value(getattr(entity, fi.name))))
            for fi in entity_cls._fields_.values()
            if not fi.spec.primary_key and not fi.spec.volatile and fi.name not in scalar_pk_names
        ]
        # Also update FK values from Single relations (owning side),
        # but skip relation fields that are part of the composite PK (they are identity)
        pk_rel_names = {f for f in entity_cls._pk_fields_ if f not in entity_cls._fields_}
        for ri in entity_cls._relations_.values():
            if ri.spec.kind == RelationKind.SINGLE and ri.name not in pk_rel_names:
                fk_col = ri.spec.column or f"{ri.name}_id"
                fk_val = entity.__dict__.get(f"_{ri.name}_id")
                assignments.append((fk_col, Param(value=fk_val)))
        # Option A — per-field optimistic concurrency check.
        # Only columns the caller actually READ (via descriptor __get__) and that
        # exist in _dbvals_ are checked; PK and untracked columns are silently skipped.
        where = _build_pk_where(entity_cls, pk_val)
        use_optimistic = bool(read_cols_snapshot) and dbvals is not None
        if use_optimistic:
            # Respect db_session(optimistic=False) to let callers opt out.
            from nextorm.session import _get_session_stack  # noqa: PLC0415

            session = _get_session_stack().current
            if session is not None and not session.optimistic:
                use_optimistic = False
        if use_optimistic:
            for col in read_cols_snapshot:
                if col not in dbvals:  # type: ignore[operator]  # PK / unknown — skip
                    continue
                orig_val = dbvals[col]  # type: ignore[index]
                if orig_val is None:
                    where = BinOp(where, "AND", BinOp(ColumnRef(col), "IS", Literal(None)))
                else:
                    where = BinOp(where, "AND", BinOp(ColumnRef(col), "=", Param(value=orig_val)))
        stmt = Update(
            table=table.name,
            assignments=tuple(assignments),
            where=where,
        )
        assert self._builder is not None
        sql, params = self._builder.render(stmt)
        rows_affected = self._execute_dml(sql, params)
        if use_optimistic and rows_affected == 0:
            raise OptimisticCheckError(
                f"Concurrent update detected for {entity_cls.__name__!r} "
                f"pk={pk_val!r}: row was modified by another transaction."
            )

    # ------------------------------------------------------------------
    # Guard
    # ------------------------------------------------------------------

    def _require_mapped(self, entity_cls: type[Entity]) -> None:
        """Raise :exc:`RuntimeError` if *entity_cls* is not in the schema."""
        if entity_cls._table_name_ not in self._schema:
            raise RuntimeError(
                f"Entity {entity_cls.__name__!r} is not in the mapped schema. "
                "Call generate_mapping() first."
            )

    # ------------------------------------------------------------------
    # Relation validation
    # ------------------------------------------------------------------

    def _validate_relations(self) -> None:
        """Validate that all ``Set[T]`` relations have proper back-references.

        Raises :exc:`~nextorm.exceptions.MappingError` on:

        * A ``Set[T]`` with no back-reference on ``T`` (missing ``Single``
          or ``Set`` on the other side).
        * Multiple relations between the same two entities when neither side
          carries ``reverse=`` to disambiguate which back-reference to use.
        """
        entities = self._effective_entities()
        entity_by_name: dict[str, EntityMeta] = {e.__name__.lower(): e for e in entities}

        def _resolve(target: type[Entity] | str | typing.ForwardRef | None) -> EntityMeta | None:
            if target is None:  # pragma: no cover
                return None
            if isinstance(target, str):  # pragma: no cover
                return entity_by_name.get(target.lower())
            if isinstance(target, typing.ForwardRef):
                return entity_by_name.get(target.__forward_arg__.lower())
            return cast("EntityMeta", target)

        for entity_cls in entities:
            for ri in entity_cls._relations_.values():
                if ri.spec.kind != RelationKind.SET:
                    continue
                target_cls = _resolve(ri.spec.target)
                if target_cls is None:
                    # Unresolvable forward reference — not a validation error
                    continue

                # Collect all back-refs on target_cls pointing at entity_cls
                back_refs = [
                    r
                    for r in target_cls._relations_.values()
                    if _resolve(r.spec.target) is entity_cls
                ]

                if not back_refs:
                    from nextorm.entity import _target_name  # noqa: PLC0415

                    target_name = _target_name(ri.spec.target) or repr(ri.spec.target)
                    raise MappingError(
                        f"{entity_cls.__name__}.{ri.name}: Set[{target_name}] requires a "
                        f"back-reference on {target_name} pointing at {entity_cls.__name__}. "
                        f"Add Single[{entity_cls.__name__}] or "
                        f"Set[{entity_cls.__name__}] to {target_name}."
                    )

                # Ambiguity: multiple back-refs on the same target without reverse=
                if len(back_refs) > 1 and ri.spec.reverse is None:
                    ambiguous = [r.name for r in back_refs if r.spec.reverse is None]
                    if len(ambiguous) > 1:  # pragma: no branch
                        raise MappingError(
                            f"{entity_cls.__name__}.{ri.name}: ambiguous — "
                            f"{target_cls.__name__} has multiple back-references to "
                            f"{entity_cls.__name__} ({', '.join(ambiguous)}). "
                            f"Add reverse='<attr>' on one side to disambiguate."
                        )
