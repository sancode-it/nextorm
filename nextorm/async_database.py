"""Async-first database API — :class:`AsyncDatabase` and :class:`AsyncQuerySet`.

Usage::

    from nextorm import AsyncDatabase, Entity, Req


    class User(Entity):
        name: Req[str]
        email: Req[str]


    db = AsyncDatabase(entities=[User])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)

    u = User(name="alice", email="alice@example.com")
    await db.asave(u)

    results = await db.aselect(User).fetch_all()
    await db.adelete_instance(u)
    await db.close()
"""

from __future__ import annotations

import contextlib
import sys
import time
import typing
from typing import IO, TYPE_CHECKING, Any, cast, overload

from nextorm.database import (
    _DDL_RENDERERS,
    _build_pk_where,
    _database_registry,
    _get_pk_val,
)
from nextorm.debug import (
    QueryStat,
    _global_stats_lock,
    _print_sql,
    _record_async_capture,
    global_stats,
)
from nextorm.entity import (
    _LAZY_SENTINEL,
    Entity,
    EntityMeta,
    _entity_registry,
    _LazyType,
)
from nextorm.exceptions import MappingError, OptimisticCheckError
from nextorm.fields import (
    RelationKind,
    _generate_ulid,
    _generate_uuid7,
    _serialize_value,
)
from nextorm.providers.base import (
    AsyncConnection,
    AsyncProvider,
    get_async_provider,
)
from nextorm.schema.builder import build_schema
from nextorm.schema.core import Table
from nextorm.schema.ddl import DDLRenderer
from nextorm.sql.builder import PARAM_STYLE_BUILDERS, SQLBuilder, SQLiteBuilder
from nextorm.sql.nodes import (
    Alias,
    BinOp,
    ColumnRef,
    Delete,
    FunctionCall,
    Insert,
    Literal,
    OrderItem,
    Param,
    Select,
    SqlNode,
    Star,
    Update,
)

__all__ = ["AsyncDatabase", "AsyncQuerySet"]

if TYPE_CHECKING:
    from collections.abc import Callable

    from nextorm.expr import ColumnExpr as _ColumnExprT


class AsyncDatabase:
    """Async counterpart to :class:`~nextorm.database.Database`.

    All I/O methods are coroutines.  Use ``await db.bind(...)`` etc.

    Parameters
    ----------
    entities:
        Explicit list of entity classes to include.  When omitted, *all*
        registered :class:`~nextorm.entity.Entity` subclasses are used.
    """

    #: Set to ``True`` so that :class:`~nextorm.entity.FieldDescriptor` can
    #: detect async context and raise a helpful error instead of blocking.
    _is_async: bool = True

    def __init__(self, entities: list[type[Entity]] | None = None) -> None:
        self._entities: list[type[Entity]] | None = list(entities) if entities is not None else None
        self._provider: str | None = None
        self._connect_args: tuple[Any, ...] = ()
        self._connect_kwargs: dict[str, Any] = {}
        self._renderer: DDLRenderer | None = None
        self._async_provider_instance: AsyncProvider | None = None
        self._builder: SQLBuilder | None = None
        self._connection: AsyncConnection | None = None
        self._schema: dict[str, Table] = {}
        self._bound: bool = False
        self._last_sql: str = ""
        self._local_stats: dict[str, QueryStat] = {}

    async def load_lazy_field(self, entity: Entity, field_name: str) -> Any:
        """Asynchronously load a single lazy field and cache the result on the entity.

        This must be used for lazy fields when the entity was loaded via
        :class:`AsyncDatabase`; synchronous access raises :exc:`RuntimeError`::

            article = await db.aselect(Article).fetch_one()
            body = await db.load_lazy_field(article, "body")
            # or equivalently:
            body = await db.load_lazy_field(article, "body")  # cached on second call
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
        rows = await self._execute(sql, params)
        value = rows[0][0] if rows else None
        # Cache on the entity so subsequent access doesn't re-query
        attr_key = f"_field_{field_name}"
        vars(entity)[attr_key] = value
        return value

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> AsyncDatabase:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def __del__(self) -> None:
        """Best-effort synchronous cleanup; avoids ResourceWarning from unclosed connections."""
        conn = getattr(self, "_connection", None)
        if conn is None:
            return
        self._connection = None
        inner = getattr(conn, "_conn", None)
        if hasattr(inner, "_thread"):
            # aiosqlite path: set _connection=None to suppress aiosqlite's own
            # ResourceWarning and prevent a double-close in close_and_stop(), then
            # call stop() to send the shutdown sentinel to the worker thread, and
            # join() so the thread finishes before the event loop can close.
            with contextlib.suppress(Exception):
                inner._connection = None  # type: ignore[union-attr]
            with contextlib.suppress(Exception):
                inner.stop()  # type: ignore[union-attr]
            with contextlib.suppress(Exception):
                inner._thread.join(timeout=0.5)  # type: ignore[union-attr]
        else:  # pragma: no cover
            raw = getattr(inner, "_conn", None)
            if raw is not None and hasattr(raw, "close"):
                with contextlib.suppress(Exception):
                    raw.close()

    # ------------------------------------------------------------------
    # Entity registration
    # ------------------------------------------------------------------

    def register(self, *entity_classes: type[Entity]) -> None:
        """Explicitly add entity classes to this database."""
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
        return {cls.__name__: cls for cls in self._effective_entities()}

    def _effective_entities(self) -> list[type[Entity]]:
        if self._entities is not None:
            return list(self._entities)
        return sorted(
            (cls for cls in _entity_registry),  # type: ignore[misc]
            key=lambda c: c.__name__,
        )

    # ------------------------------------------------------------------
    # Provider binding
    # ------------------------------------------------------------------

    async def bind(self, provider: str, *args: Any, **kwargs: Any) -> None:
        """Bind the database to a named async provider and open a connection.

        Parameters
        ----------
        provider:
            Registered provider name: ``"sqlite"``, ``"postgres"``, or
            ``"mariadb"``.
        *args* / **kwargs**:
            Connection arguments forwarded to the async provider
            (e.g. the database path for aiosqlite, or the DSN for PostgreSQL).

        Example::

            db = AsyncDatabase(entities=[User])
            await db.bind("sqlite", ":memory:")
            await db.generate_mapping(create_tables=True)
        """
        from nextorm.providers.base import _PROVIDER_REGISTRY  # noqa: PLC0415

        if provider not in _PROVIDER_REGISTRY:
            raise ValueError(
                f"Unknown provider {provider!r}. Available: {sorted(_PROVIDER_REGISTRY)}"
            )
        self._provider = provider
        self._connect_args = args
        self._connect_kwargs = kwargs
        if provider in _DDL_RENDERERS:  # pragma: no branch
            self._renderer = _DDL_RENDERERS[provider]()
        self._async_provider_instance = get_async_provider(provider)()
        builder_cls = PARAM_STYLE_BUILDERS.get(
            self._async_provider_instance.param_style, SQLiteBuilder
        )
        self._builder = builder_cls()
        self._connection = await self._async_provider_instance.connect(*args, **kwargs)
        self._bound = True

    async def close(self) -> None:
        """Close the async connection (if open)."""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
        with contextlib.suppress(ValueError):
            _database_registry.remove(self)

    async def disconnect(self) -> None:
        """Close the async connection without full teardown.

        Async counterpart of :meth:`~nextorm.database.Database.disconnect`.
        The :class:`AsyncDatabase` object remains usable after re-binding.

        Example::

            await db.disconnect()  # release the connection handle
        """
        await self.close()

    async def unbind(self) -> None:
        """Close the connection and clear the provider binding.

        Async counterpart of :meth:`~nextorm.database.Database.unbind`.
        After calling this you must ``await db.bind(...)`` again before
        issuing queries.
        """
        await self.close()
        self._provider = None
        self._connect_args = ()
        self._connect_kwargs = {}
        self._renderer = None
        self._async_provider_instance = None
        self._builder = None
        self._connection = None
        self._schema = {}
        self._bound = False

    def get_ddl(self) -> list[str]:
        """Return the DDL statements captured during :meth:`generate_mapping`.

        Async counterpart of :meth:`~nextorm.database.Database.get_ddl`.
        """
        return list(getattr(self, "_ddl_statements", []))

    async def migrate(self) -> list[str]:
        """Apply pending schema changes to the live database.

        Async counterpart of :meth:`~nextorm.database.Database.migrate`.

        Introspects the current database schema, computes the diff against
        the entity-derived target schema, and executes each pending DDL
        statement.  Returns the list of SQL statements that were executed.
        An empty list means the database is already up to date.

        Raises :exc:`RuntimeError` if not bound or if :meth:`generate_mapping`
        has not been called.

        .. note::
           Only the ``"sqlite"`` async provider is currently supported.
           For ``"postgres"`` or ``"mariadb"`` use the sync
           :meth:`~nextorm.database.Database.migrate`.
        """
        from nextorm.schema.diff import diff_schemas  # noqa: PLC0415

        if not self._bound:
            raise RuntimeError("AsyncDatabase is not bound to a provider. Call await bind() first.")
        if not self._schema:
            raise RuntimeError("Schema is empty. Call await generate_mapping() before migrate().")
        assert self._async_provider_instance is not None
        assert self._renderer is not None
        conn = self._ensure_connection()

        if self._provider == "sqlite":
            from nextorm.schema.introspect import (
                async_introspect_sqlite,  # noqa: PLC0415
            )

            current = await async_introspect_sqlite(conn)
        else:  # pragma: no cover
            raise RuntimeError(
                f"AsyncDatabase.migrate() is not yet supported for the {self._provider!r} provider. "
                "Use the sync Database.migrate() instead."
            )

        ops = diff_schemas(current, self._schema)
        stmts = [self._renderer.render(op) for op in ops]
        if stmts:
            await self._async_provider_instance.execute_ddl(conn, stmts)
        return stmts

    @property
    def is_bound(self) -> bool:
        """Return ``True`` when :meth:`bind` has been called successfully."""
        return self._bound

    def _ensure_connection(self) -> AsyncConnection:
        if self._connection is None:
            raise RuntimeError("Database is not connected. Call await db.bind() first.")
        return self._connection

    # ------------------------------------------------------------------
    # Schema mapping
    # ------------------------------------------------------------------

    async def generate_mapping(
        self, *, create_tables: bool = False, validate_relations: bool = True
    ) -> None:
        """Build the internal schema and optionally create database tables.

        Async counterpart of :meth:`~nextorm.database.Database.generate_mapping`.

        Parameters
        ----------
        create_tables:
            When ``True``, execute ``CREATE TABLE`` DDL for every entity in
            the schema.
        validate_relations:
            When ``True``, every declared ``Set[T]`` relation is validated.
            See :meth:`~nextorm.database.Database.generate_mapping` for details.
        """
        if not self._bound:
            raise RuntimeError("AsyncDatabase is not bound to a provider. Call await bind() first.")
        if validate_relations:
            self._validate_relations()

        self._schema = build_schema(self._effective_entities())

        # Register so Entity.__init__ can auto-locate this database.
        if self not in _database_registry:
            _database_registry.append(self)

        if create_tables:
            assert self._renderer is not None
            ddl_stmts = [self._renderer.create_table(t) for t in self._schema.values()]
            assert self._async_provider_instance is not None
            conn = self._ensure_connection()
            await self._async_provider_instance.execute_ddl(conn, ddl_stmts)

    @property
    def schema(self) -> dict[str, Table]:
        """Return a copy of the current table schema (populated by :meth:`generate_mapping`)."""
        return dict(self._schema)

    async def create_tables(self) -> None:
        """Create tables for all entities if they don't already exist.

        Raises :exc:`RuntimeError` if the database is not bound or mapped.

        Example::

            await db.bind("sqlite", ":memory:")
            await db.generate_mapping()
            await db.create_tables()
        """
        if not self._schema:
            raise RuntimeError("Database is not mapped. Call generate_mapping() first.")
        assert self._renderer is not None
        assert self._async_provider_instance is not None

        conn = self._ensure_connection()
        statements = [self._renderer.create_table(table) for table in self._schema.values()]
        await self._async_provider_instance.execute_ddl(conn, statements)

    async def drop_table(
        self, table_name: str, *, if_exists: bool = False, with_all_data: bool = False
    ) -> None:
        """Drop a specific table from the database.

        Parameters
        ----------
        table_name:
            Name of the table to drop (case-sensitive).
        if_exists:
            When ``True``, do not raise an error if the table doesn't exist.
        with_all_data:
            When ``False``, raises an error if the table contains data.
            When ``True``, drops the table regardless of content.

        Raises :exc:`RuntimeError` if the table contains data and
        ``with_all_data`` is ``False``.
        """
        if not self._bound:
            raise RuntimeError("Database is not bound. Call await bind() first.")
        assert self._renderer is not None
        assert self._async_provider_instance is not None

        conn = self._ensure_connection()
        if not with_all_data:
            try:
                rows = await self._execute(f"SELECT 1 FROM {table_name} LIMIT 1", [])
                if rows:
                    raise RuntimeError(
                        f"Table {table_name} is not empty. "
                        f"Set with_all_data=True to drop table with data."
                    )
            except RuntimeError:
                raise
            except Exception:
                pass

        statement = self._renderer.drop_table(table_name) if if_exists else f"DROP TABLE {table_name}"
        await self._async_provider_instance.execute_ddl(conn, [statement])

    async def drop_all_tables(self, *, with_all_data: bool = False) -> None:
        """Drop all tables associated with this database.

        Parameters
        ----------
        with_all_data:
            When ``False``, raises an error if any table contains data.
            When ``True``, drops all tables regardless of content.

        Raises :exc:`RuntimeError` if a table contains data and
        ``with_all_data`` is ``False``.

        Example::

            await db.drop_all_tables(with_all_data=True)
        """
        if not self._schema:
            raise RuntimeError("Database is not mapped. Call generate_mapping() first.")
        assert self._renderer is not None
        assert self._async_provider_instance is not None

        conn = self._ensure_connection()
        if not with_all_data:
            for table_name in self._schema:
                try:
                    rows = await self._execute(f"SELECT 1 FROM {table_name} LIMIT 1", [])
                    if rows:
                        raise RuntimeError(
                            f"Table {table_name} is not empty. "
                            f"Set with_all_data=True to drop tables with data."
                        )
                except RuntimeError:
                    raise
                except Exception:
                    pass

        statements = [
            self._renderer.drop_table(table_name)
            for table_name in reversed(list(self._schema.keys()))
        ]
        await self._async_provider_instance.execute_ddl(conn, statements)

    # ------------------------------------------------------------------
    # Relation validation (mirrors Database._validate_relations)
    # ------------------------------------------------------------------

    def _validate_relations(self) -> None:
        entities = self._effective_entities()
        entity_by_name: dict[str, type[Entity]] = {e.__name__.lower(): e for e in entities}

        def _resolve(
            target: type[Entity] | str | typing.ForwardRef | _LazyType | None,
        ) -> type[Entity] | None:
            if target is None:  # pragma: no cover
                return None
            if isinstance(target, str):  # pragma: no cover
                return entity_by_name.get(target.lower())
            if isinstance(target, (typing.ForwardRef, _LazyType)):
                return entity_by_name.get(target.__forward_arg__.lower())
            return target

        for entity_cls in entities:
            for ri in entity_cls._relations_.values():
                if ri.spec.kind != RelationKind.SET:
                    continue
                target_cls = _resolve(ri.spec.target)
                if target_cls is None:
                    continue
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
                        f"back-reference on {target_name} pointing at {entity_cls.__name__}."
                    )

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def aselect[ET: Entity](self, entity_class: type[ET]) -> AsyncQuerySet[ET]:
        """Return an :class:`AsyncQuerySet` for *entity_class*.

        :meth:`generate_mapping` must have been called first.

        Example::

            users = await db.aselect(User).filter(User.active == True).fetch_all()
        """
        if not self._schema:
            raise RuntimeError("Schema is empty. Call await generate_mapping() before aselect().")
        table_name = entity_class._table_name_
        table = self._schema.get(table_name)
        if table is None:
            raise RuntimeError(f"Entity {entity_class.__name__!r} is not in the mapped schema.")
        assert self._builder is not None
        qs: AsyncQuerySet[ET] = AsyncQuerySet(entity_class, table, self, self._builder)
        # STI: automatically filter by discriminator value for child entities
        disc_val = entity_class._discriminator_val_
        if disc_val is not None and entity_class._sti_parent_ is not None:
            disc_col = entity_class._sti_parent_._discriminator_col_
            if disc_col is not None:  # pragma: no branch
                from nextorm.sql.nodes import BinOp, ColumnRef, Param  # noqa: PLC0415

                qs = qs.filter(BinOp(ColumnRef(disc_col), "=", Param(value=disc_val)))
        return qs

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def asave(self, entity: Entity) -> None:
        """Insert or update *entity* asynchronously.

        Async counterpart of :meth:`~nextorm.database.Database.save`.
        PKs that are ``None`` trigger an ``INSERT``; a set PK triggers an
        ``UPDATE``.  The entity's lifecycle hooks are called in both cases.

        Example::

            async with db_session:
                u = User(name="alice", age=30)
                await db.asave(u)
                print(u.id)  # assigned by DB
        """
        entity_cls = type(entity)
        self._require_mapped(entity_cls)
        table = self._schema[entity_cls._table_name_]
        pk_val = _get_pk_val(entity)

        # For composite PKs or user-supplied single PKs, use _dbvals_ presence to
        # distinguish new (not-yet-inserted) from existing.  We cannot use _db_
        # because Entity.__init__ sets _db_ immediately when created inside a
        # db_session (before the first actual DB write).
        # Even for auto-increment PKs, if the user manually set the PK, we need to
        # check _dbvals_ to know whether to INSERT or UPDATE.
        is_new = pk_val is None
        if not is_new:
            # Check _dbvals_ to distinguish new from existing for all cases:
            # - Composite PKs (user supplies all values)
            # - Single non-auto PKs (user supplies the value)
            # - Single auto PKs with user-set values (also need to INSERT, not UPDATE)
            if entity_cls._pk_field_ is None:
                # Composite PK: check _dbvals_ to know if this is a new entity
                is_new = "_dbvals_" not in vars(entity)
            else:
                # Single-field PK: check _dbvals_ for both auto and non-auto PKs
                # because user might manually set an auto PK
                is_new = "_dbvals_" not in vars(entity)

        try:
            if is_new:
                entity.before_insert()
                # If user manually set an auto PK, include it in the INSERT statement
                include_auto_pk = pk_val is not None and entity_cls._pk_field_ is not None
                pk_field = (
                    entity_cls._fields_.get(entity_cls._pk_field_)
                    if entity_cls._pk_field_ is not None
                    else None
                )
                include_auto_pk = include_auto_pk and (pk_field is not None and pk_field.spec.auto)
                await self._do_insert(entity, entity_cls, table, include_auto_pk=include_auto_pk)
                entity.after_insert()
            else:
                entity.before_update()
                await self._do_update(entity, entity_cls, table, pk_val)
                entity.after_update()
        except Exception:
            # Save failed — remove entity from session tracking so flush() won't retry.
            from nextorm.session import _get_session_stack  # noqa: PLC0415

            _cache = _get_session_stack().current
            if _cache is not None:
                _cache.unmark_dirty(entity)
                _cache.unschedule_save(entity)
            raise

        vars(entity)["_db_"] = self
        # Update session identity map
        from nextorm.session import _get_session_stack  # noqa: PLC0415

        cache = _get_session_stack().current
        new_pk = _get_pk_val(entity)
        if cache is not None and new_pk is not None:
            cache.put(entity, new_pk)
            cache.unmark_dirty(entity)
            cache.unschedule_save(entity)

    async def ainsert(self, entity: Entity) -> None:
        """Always INSERT *entity* asynchronously, even when its primary key is already set.

        Async counterpart of :meth:`~nextorm.database.Database.insert`.
        """
        entity_cls = type(entity)
        self._require_mapped(entity_cls)
        table = self._schema[entity_cls._table_name_]
        entity.before_insert()
        await self._do_insert(entity, entity_cls, table, include_auto_pk=True)
        entity.after_insert()
        vars(entity)["_db_"] = self

    async def adelete_instance(self, entity: Entity) -> None:
        """Delete *entity* from the database asynchronously.

        Async counterpart of :meth:`~nextorm.database.Database.delete_instance`.
        Raises :exc:`ValueError` when the entity has not been saved (PK is ``None``).
        """
        entity_cls = type(entity)
        self._require_mapped(entity_cls)
        pk_val = _get_pk_val(entity)
        pk_fields = entity_cls._pk_fields_
        if not pk_fields:
            raise RuntimeError(f"Entity {entity_cls.__name__!r} has no primary-key field.")
        if pk_val is None:
            raise ValueError(f"Cannot delete {entity_cls.__name__!r}: primary key is None.")
        table = self._schema[entity_cls._table_name_]
        stmt = Delete(
            table=table.name,
            where=_build_pk_where(entity_cls, pk_val),
        )
        assert self._builder is not None
        sql, params = self._builder.render(stmt)
        entity.before_delete()
        await self._execute_dml(sql, params)
        # Clear all PK fields
        for fname in pk_fields:
            if fname in entity_cls._fields_:
                setattr(entity, fname, None)
            else:
                vars(entity).pop(f"_{fname}_id", None)
        entity.after_delete()

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    async def acommit(self) -> None:
        """Flush pending changes then commit the current database transaction.

        Calls :meth:`aflush` first so all pending inserts/updates are written,
        then finalises the underlying connection's transaction (PonyORM-style
        integrated flush).
        """
        await self.aflush()
        await self._acommit_transaction()

    async def _acommit_transaction(self) -> None:
        """Issue SQL COMMIT without an implicit flush.

        Used by the staged-commit logic in the async session-exit path so that
        flush and commit can be separated (flush all, commit primary, commit secondaries).
        """
        conn = self._ensure_connection()
        await conn.commit()

    async def _arollback_transaction(self) -> None:
        """Issue SQL ROLLBACK without clearing the session cache.

        Used during partial-commit cleanup where the session cache will be
        cleared separately after all databases are processed.
        """
        conn = self._ensure_connection()
        await conn.rollback()

    async def arollback(self) -> None:
        """Roll back the current database transaction and clear the session cache.

        Discards uncommitted work on the active connection and wipes the
        identity map of the current :func:`~nextorm.session.db_session`.
        """
        await self._arollback_transaction()
        from nextorm.session import _get_session_stack  # noqa: PLC0415

        cache = _get_session_stack().current
        if cache is not None:
            cache.clear()

    async def aflush(self) -> None:
        """Write all pending dirty and new objects in the current session to the DB.

        Iterates the current :func:`~nextorm.session.db_session` cache and
        calls :meth:`asave` on every object that has been scheduled for INSERT
        or marked dirty.

        When called outside a session this is a no-op.
        """
        from nextorm.session import _get_session_stack  # noqa: PLC0415

        cache = _get_session_stack().current
        if cache is None:
            return
        for entity in list(cache.objects_to_save):
            entity_db = vars(entity).get("_db_")
            # Save if this is the right DB, or if the entity has no DB yet.
            if entity_db is None or entity_db is self:
                await self.asave(entity)
        for entity in list(cache.dirty_objects):
            entity_db = vars(entity).get("_db_")
            if entity_db is None or entity_db is self:
                await self.asave(entity)

    # ------------------------------------------------------------------
    # Low-level query execution
    # ------------------------------------------------------------------

    def get_connection(self) -> Any:
        """Return the raw underlying async connection object.

        The type depends on the active provider (e.g. ``aiosqlite.Connection``
        for async SQLite).  Use this as an escape hatch for provider-specific
        features.
        """
        conn = self._ensure_connection()
        raw = getattr(conn, "_conn", conn)
        return raw

    async def execute(self, sql: str, *args: Any) -> int:
        """Execute arbitrary SQL asynchronously and return the number of affected rows.

        Pending entity inserts and updates in the current
        :func:`~nextorm.session.db_session` are flushed before the statement
        is executed, so that the SQL operates on up-to-date data.

        *args* are bound as positional parameters::

            await db.execute("DELETE FROM session WHERE expires_at < ?", cutoff)
        """
        await self.aflush()
        return await self._execute_dml(sql, list(args))

    async def select_raw(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """Execute a raw SELECT asynchronously and return results as dicts.

        Column names are taken from the cursor description::

            rows = await db.select_raw(
                "SELECT u.name, COUNT(o.id) AS cnt "
                "FROM user u LEFT JOIN order o ON o.user_id = u.id "
                "GROUP BY u.id"
            )
        """
        conn = self._ensure_connection()
        cur = await conn.cursor()
        await cur.execute(sql, list(args))
        rows = await cur.fetchall()
        desc = cur.description
        if not rows or desc is None:
            return []
        col_names = [d[0] for d in desc]
        return [dict(zip(col_names, row, strict=False)) for row in rows]

    async def _execute(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if not getattr(
            self, "_flushing_", False
        ):  # pragma: no branch — async before_insert is sync; recursive _execute is unreachable
            from nextorm.session import _get_session_stack  # noqa: PLC0415

            cache = _get_session_stack().current
            if cache is not None and (cache.objects_to_save or cache.dirty_objects):
                saving_set: set[int] = getattr(self, "_saving_", None) or set()
                pending = [
                    e
                    for e in cache.objects_to_save
                    if vars(e).get("_db_") is self
                    and "_dbvals_" not in vars(e)
                    and id(e) not in saving_set
                ]
                dirty = [
                    e
                    for e in cache.dirty_objects
                    if vars(e).get("_db_") is self and id(e) not in saving_set
                ]
                if pending or dirty:
                    self._flushing_: bool = True
                    try:
                        for entity in pending:
                            await self.asave(entity)
                        for entity in dirty:
                            await self.asave(entity)
                    finally:
                        self._flushing_ = False
        self._last_sql = sql
        _print_sql(sql, params)
        _record_async_capture(sql, params)
        conn = self._ensure_connection()
        cur = await conn.cursor()
        t0 = time.perf_counter()
        await cur.execute(sql, params)
        rows = list(await cur.fetchall())
        self._record_stat(sql, time.perf_counter() - t0)
        return rows

    async def _execute_described(
        self, sql: str, params: list[Any]
    ) -> tuple[list[tuple[Any, ...]], list[str]]:
        """Execute *sql* and return ``(rows, column_names)``."""
        self._last_sql = sql
        _print_sql(sql, params)
        _record_async_capture(sql, params)
        conn = self._ensure_connection()
        cur = await conn.cursor()
        t0 = time.perf_counter()
        await cur.execute(sql, params)
        rows = list(await cur.fetchall())
        self._record_stat(sql, time.perf_counter() - t0)
        col_names = [d[0] for d in cur.description] if cur.description else []
        return rows, col_names

    async def _execute_dml(self, sql: str, params: list[Any]) -> int:
        self._last_sql = sql
        _print_sql(sql, params)
        _record_async_capture(sql, params)
        conn = self._ensure_connection()
        cur = await conn.cursor()
        t0 = time.perf_counter()
        await cur.execute(sql, params)
        count = cur.rowcount
        from nextorm.session import _get_session_stack  # noqa: PLC0415

        if _get_session_stack().current is None:
            await conn.commit()
        self._record_stat(sql, time.perf_counter() - t0)
        return count

    async def _execute_insert(self, sql: str, params: list[Any]) -> int | None:
        self._last_sql = sql
        _print_sql(sql, params)
        _record_async_capture(sql, params)
        conn = self._ensure_connection()
        cur = await conn.cursor()
        t0 = time.perf_counter()
        await cur.execute(sql, params)
        rowid = cur.lastrowid
        from nextorm.session import _get_session_stack  # noqa: PLC0415

        if _get_session_stack().current is None:
            await conn.commit()
        self._record_stat(sql, time.perf_counter() - t0)
        return rowid

    @property
    def last_sql(self) -> str:
        """The last SQL string sent to the database."""
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
        statistics across multiple :class:`AsyncDatabase` instances.
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

    # ------------------------------------------------------------------
    # INSERT / UPDATE helpers
    # ------------------------------------------------------------------

    async def _do_insert(
        self,
        entity: Entity,
        entity_cls: type[Entity],
        table: Table,
        *,
        include_auto_pk: bool = False,
    ) -> None:
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
        cols_and_vals: list[tuple[str, Any]] = []
        for fi in entity_cls._fields_.values():
            if (
                fi.spec.primary_key
                and fi.spec.auto
                and (not include_auto_pk or getattr(entity, fi.name) is None)
            ):
                continue
            val = getattr(entity, fi.name)
            # If field is str or LongStr and not nullable, None → ""
            if val is None and issubclass(fi.py_type, str) and not fi.spec.nullable:
                val = ""
            cols_and_vals.append((fi.spec.column or fi.name, _serialize_value(val)))
        # Also persist FK values from Single relations (owning side).
        # Skip FK columns that don't exist in the table (non-owning side of O2O back-refs).
        table_col_names = table.column_names()
        for ri in entity_cls._relations_.values():
            if ri.spec.kind == RelationKind.SINGLE:
                # First check for explicit FK value set via _<name>_id
                fk_val = entity.__dict__.get(f"_{ri.name}_id")
                # If not found, try to extract from the related entity
                if fk_val is None:
                    related_entity = entity.__dict__.get(f"_{ri.name}_obj")
                    if related_entity is not None and isinstance(related_entity, Entity):
                        from nextorm.database import _get_pk_val  # noqa: PLC0415

                        fk_val = _get_pk_val(related_entity)
                if isinstance(fk_val, tuple):
                    # Composite PK FK — expand to multiple column entries
                    from nextorm.entity import (
                        _derive_composite_fk_cols,  # noqa: PLC0415
                    )

                    fk_col_names = ri.spec.columns or _derive_composite_fk_cols(
                        ri.name, ri.spec.target
                    )
                    _fk_tuple = cast("tuple[Any, ...]", fk_val)  # type: ignore[redundant-cast]
                    for col_name, val in zip(fk_col_names, _fk_tuple, strict=False):
                        if col_name in table_col_names:  # pragma: no branch
                            cols_and_vals.append((col_name, val))
                else:
                    fk_col = (
                        (ri.spec.columns[0] if ri.spec.columns else None)
                        or ri.spec.column
                        or f"{ri.name}_id"
                    )
                    if fk_col not in table_col_names:
                        continue
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
        rowid = await self._execute_insert(sql, params)
        # Write the auto-generated PK back only for DB-side auto-increment PKs.
        # UUID / ULID PKs (spec.uuid_auto set, spec.auto=False) are already written
        # above before the INSERT — do not overwrite them with lastrowid.
        if single_pk_field is not None and rowid is not None:  # pragma: no branch
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
                fk_val = vars(entity).get(f"_{ri.name}_id")
                if isinstance(fk_val, tuple):
                    from nextorm.entity import (
                        _derive_composite_fk_cols,  # noqa: PLC0415
                    )

                    fk_col_names = ri.spec.columns or _derive_composite_fk_cols(
                        ri.name, ri.spec.target
                    )
                    _fk_tuple2 = cast("tuple[Any, ...]", fk_val)  # type: ignore[redundant-cast]
                    for col_name, val in zip(fk_col_names, _fk_tuple2, strict=False):
                        if col_name in table_col_names:  # pragma: no branch
                            dbvals[col_name] = val
                else:
                    fk_col = (
                        (ri.spec.columns[0] if ri.spec.columns else None)
                        or ri.spec.column
                        or f"{ri.name}_id"
                    )
                    if fk_col not in table_col_names:
                        continue
                    dbvals[fk_col] = fk_val
        vars(entity)["_dbvals_"] = dbvals
        if "_read_cols_" not in vars(entity):  # pragma: no branch
            vars(entity)["_read_cols_"] = set()

    async def _do_update(
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

        assignments: list[tuple[str, Param]] = [
            (
                fi.spec.column or fi.name,
                Param(value=_serialize_value(getattr(entity, fi.name))),
            )
            for fi in entity_cls._fields_.values()
            if not fi.spec.primary_key and not fi.spec.volatile
        ]
        pk_rel_names = {f for f in entity_cls._pk_fields_ if f not in entity_cls._fields_}
        table_col_names = table.column_names()
        for ri in entity_cls._relations_.values():
            if ri.spec.kind == RelationKind.SINGLE and ri.name not in pk_rel_names:
                # First check for explicit FK value set via _<name>_id
                fk_val = entity.__dict__.get(f"_{ri.name}_id")
                # If not found, try to extract from the related entity
                if fk_val is None:
                    related_entity = entity.__dict__.get(f"_{ri.name}_obj")
                    if related_entity is not None and isinstance(related_entity, Entity):
                        from nextorm.database import _get_pk_val  # noqa: PLC0415

                        fk_val = _get_pk_val(related_entity)
                if isinstance(fk_val, tuple):
                    # Composite PK FK — expand to multiple column entries
                    from nextorm.entity import (
                        _derive_composite_fk_cols,  # noqa: PLC0415
                    )

                    fk_col_names = ri.spec.columns or _derive_composite_fk_cols(
                        ri.name, ri.spec.target
                    )
                    _fk_tuple = cast("tuple[Any, ...]", fk_val)  # type: ignore[redundant-cast]
                    for col_name, val in zip(fk_col_names, _fk_tuple, strict=False):
                        if col_name in table_col_names:  # pragma: no branch
                            assignments.append((col_name, Param(value=val)))
                else:
                    fk_col = (
                        (ri.spec.columns[0] if ri.spec.columns else None)
                        or ri.spec.column
                        or f"{ri.name}_id"
                    )
                    if fk_col not in table_col_names:
                        continue
                    assignments.append((fk_col, Param(value=fk_val)))
        # Option A — per-field optimistic concurrency check (async path).
        # Only columns in _dbvals_ (non-PK, non-volatile) are eligible.
        where = _build_pk_where(entity_cls, pk_val)
        use_optimistic = bool(read_cols_snapshot) and dbvals is not None
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
        rows_affected = await self._execute_dml(sql, params)
        if use_optimistic and rows_affected == 0:
            raise OptimisticCheckError(
                f"Concurrent update detected for {entity_cls.__name__!r} "
                f"pk={pk_val!r}: row was modified by another transaction."
            )

    # ------------------------------------------------------------------
    # Guard
    # ------------------------------------------------------------------

    def _require_mapped(self, entity_cls: type[Entity]) -> None:
        if entity_cls._table_name_ not in self._schema:
            raise RuntimeError(
                f"Entity {entity_cls.__name__!r} is not in the mapped schema. "
                "Call await generate_mapping() first."
            )


# ---------------------------------------------------------------------------
# AsyncQuerySet
# ---------------------------------------------------------------------------


class AsyncQuerySet[ET: Entity]:
    """Async counterpart to :class:`~nextorm.query.QuerySet`.

    All terminal methods (``fetch_all``, ``fetch_one``, etc.) are coroutines.
    """

    def __init__(
        self,
        entity_class: type[ET],
        table: Table,
        db: AsyncDatabase,
        builder: SQLBuilder,
    ) -> None:
        from nextorm.query import (  # noqa: PLC0415
            _build_column_map,
            _build_explicit_column_map,
        )

        self._entity_class = entity_class
        self._table = table
        self._db = db
        self._builder = builder
        self._where: SqlNode | None = None
        self._order: tuple[OrderItem, ...] = ()
        self._lim: int | None = None
        self._off: int | None = None
        self._joins: tuple[tuple[str, str, str | None, SqlNode], ...] = ()
        self._distinct: bool = False
        self._for_update: bool = False
        self._for_update_skip_locked: bool = False
        self._lazy_field_names: frozenset[str] = frozenset(
            fi.name for fi in entity_class._fields_.values() if fi.spec.lazy
        )
        self._prefetches: tuple[str, ...] = ()
        self._explicit_columns: tuple[ColumnRef, ...] | None
        self._column_map: list[str | None]
        if self._lazy_field_names:
            self._explicit_columns, explicit_map = _build_explicit_column_map(entity_class)
            self._column_map = explicit_map
        else:
            self._explicit_columns = None
            self._column_map = _build_column_map(entity_class, table)

    def _clone(self) -> AsyncQuerySet[ET]:
        q: AsyncQuerySet[ET] = object.__new__(type(self))
        q._entity_class = self._entity_class
        q._table = self._table
        q._db = self._db
        q._builder = self._builder
        q._where = self._where
        q._order = self._order
        q._lim = self._lim
        q._off = self._off
        q._joins = self._joins
        q._distinct = self._distinct
        q._for_update = self._for_update
        q._for_update_skip_locked = self._for_update_skip_locked
        q._lazy_field_names = self._lazy_field_names
        q._explicit_columns = self._explicit_columns
        q._column_map = self._column_map
        q._prefetches = self._prefetches
        return q

    # ------------------------------------------------------------------
    # Chainable modifiers
    # ------------------------------------------------------------------

    def filter(self, *conditions: SqlNode, **kwargs: Any) -> AsyncQuerySet[ET]:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.filter`.

        Keyword arguments are treated as equality shortcuts (``field=value``).
        """
        q = self._clone()
        for cond in conditions:
            q._where = cond if q._where is None else BinOp(q._where, "AND", cond)
        for field, value in kwargs.items():
            kw_cond = BinOp(ColumnRef(field), "=", Param(value=value))
            q._where = kw_cond if q._where is None else BinOp(q._where, "AND", kw_cond)
        return q

    def where(self, predicate: Callable[[Any], Any]) -> AsyncQuerySet[ET]:
        """Narrow results using a lambda predicate.

        Async counterpart of :meth:`~nextorm.query.QuerySet.where`.
        First tries bytecode decompilation (supports M2M containment, bool
        attribute checks, method calls); falls back to proxy-based evaluation
        for simple conditions.
        """
        from nextorm.generators import DecompileError, _apply_predicate  # noqa: PLC0415
        from nextorm.query import EntityProxy  # noqa: PLC0415

        entity_cls = getattr(self._table, "entity_cls", None)
        if entity_cls is not None:  # pragma: no branch
            try:
                return _apply_predicate(self, predicate, entity_cls)  # type: ignore[no-any-return]
            except DecompileError:
                pass  # fall through to proxy-based approach

        proxy = EntityProxy(self._table.name)
        cond = predicate(proxy)
        return self.filter(cond)

    @overload
    def order_by(
        self,
        func: Callable[[Any], _ColumnExprT | OrderItem | tuple[_ColumnExprT | OrderItem, ...]],
        /,
    ) -> AsyncQuerySet[ET]: ...
    @overload
    def order_by(self, *items: OrderItem | _ColumnExprT) -> AsyncQuerySet[ET]: ...
    def order_by(self, *items: Any) -> AsyncQuerySet[ET]:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.order_by`.

        Accepts the same forms as the sync version: ``OrderItem`` / bare
        :class:`~nextorm.expr.ColumnExpr` values, or a single lambda::

            qs.order_by(lambda u: u.name)
            qs.order_by(lambda u: (u.age.desc(), u.name.asc()))
        """
        from nextorm.query import _normalize_order_items  # noqa: PLC0415

        q = self._clone()
        order, joins = _normalize_order_items(items, self._table.name, self._entity_class)
        q._order = order
        for join_spec in joins:
            q._joins = (*q._joins, join_spec)
        return q

    def limit(self, n: int) -> AsyncQuerySet[ET]:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.limit`."""
        q = self._clone()
        q._lim = n
        return q

    def offset(self, n: int) -> AsyncQuerySet[ET]:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.offset`."""
        q = self._clone()
        q._off = n
        return q

    def join(
        self,
        table_or_entity: type[Entity] | str,
        on: BinOp,
        *,
        join_type: str = "INNER",
        alias: str | None = None,
    ) -> AsyncQuerySet[ET]:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.join`."""
        if isinstance(table_or_entity, str):
            table_name = table_or_entity
        else:
            table_name = table_or_entity._table_name_
        q = self._clone()
        q._joins = (*q._joins, (join_type, table_name, alias, on))
        return q

    # ------------------------------------------------------------------
    # Prefetch
    # ------------------------------------------------------------------

    def prefetch(self, *relation_attrs: Any) -> AsyncQuerySet[ET]:
        """Declare relations to eager-load alongside the main query.

        Async counterpart of :meth:`~nextorm.query.QuerySet.prefetch`.

        After :meth:`fetch_all` executes the main SELECT, one additional query
        per prefetched relation is issued and the results are attached to the
        returned entity instances, avoiding the N+1 query problem.

        Parameters
        ----------
        \\*relation_attrs:
            Attribute descriptors from the entity class, e.g. ``Post.tags``.
            String attribute names are also accepted.

        Example::

            posts = await db.aselect(Post).prefetch(Post.comments).fetch_all()
            for post in posts:
                # post.comments is pre-populated — no extra query fired
                print(post.comments.count())
        """
        names: list[str] = []
        for attr in relation_attrs:
            if isinstance(attr, str):
                names.append(attr)
            else:
                name = getattr(attr, "name", None)
                if name is None:
                    raise ValueError(f"Cannot determine relation name from {attr!r}.")
                names.append(name)
        q = self._clone()
        q._prefetches = (*q._prefetches, *names)
        return q

    # ------------------------------------------------------------------
    # Terminal methods
    # ------------------------------------------------------------------

    async def fetch_all(self) -> list[ET]:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.fetch_all`."""
        stmt = self._build_select()
        sql, params = self._builder.render(stmt)
        rows = await self._db._execute(sql, params)
        results = [self._map_row(row) for row in rows]
        if self._prefetches:
            await self._do_async_prefetch(results)
        return results

    async def fetch_one(self) -> ET | None:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.fetch_one`."""
        stmt = self._build_select(extra_limit=1)
        sql, params = self._builder.render(stmt)
        rows = await self._db._execute(sql, params)
        if not rows:
            return None
        result = self._map_row(rows[0])
        if self._prefetches:
            await self._do_async_prefetch([result])
        return result

    async def first(self) -> ET | None:
        """Alias for :meth:`fetch_one`."""
        return await self.fetch_one()

    async def count(self) -> int:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.count`."""
        stmt = Select(
            columns=(Alias(FunctionCall("COUNT", (Star(),)), "_n"),),
            from_table=self._table.name,
            joins=self._joins,
            where=self._where,
        )
        sql, params = self._builder.render(stmt)
        rows = await self._db._execute(sql, params)
        return int(rows[0][0]) if rows else 0

    async def exists(self) -> bool:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.exists`."""
        stmt = Select(
            columns=(Literal(1),),
            from_table=self._table.name,
            joins=self._joins,
            where=self._where,
            limit=1,
        )
        sql, params = self._builder.render(stmt)
        rows = await self._db._execute(sql, params)
        return bool(rows)

    async def get(self) -> ET | None:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.get`."""
        from nextorm.exceptions import MultipleObjectsFoundError  # noqa: PLC0415

        stmt = self._build_select(extra_limit=2)
        sql, params = self._builder.render(stmt)
        rows = await self._db._execute(sql, params)
        if not rows:
            return None
        if len(rows) > 1:
            raise MultipleObjectsFoundError(
                f"get() returned more than one {self._entity_class.__name__!r}. "
                "Use fetch_all() or refine the filter."
            )
        return self._map_row(rows[0])

    async def get_or_raise(self) -> ET:
        """Async :meth:`~nextorm.query.QuerySet.get_or_raise`."""
        from nextorm.exceptions import ObjectNotFound  # noqa: PLC0415

        result = await self.get()
        if result is None:
            raise ObjectNotFound(
                f"{self._entity_class.__name__!r} matching the given filter was not found."
            )
        return result

    async def delete(self) -> int:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.delete`."""
        stmt = Delete(table=self._table.name, where=self._where)
        sql, params = self._builder.render(stmt)
        return await self._db._execute_dml(sql, params)

    async def update(self, **field_values: Any) -> int:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.update`."""
        if not field_values:
            return 0
        assignments: list[tuple[str, SqlNode]] = []
        for field_name, value in field_values.items():
            fi = self._entity_class._fields_.get(field_name)
            if fi is None:
                raise ValueError(f"{self._entity_class.__name__!r} has no field {field_name!r}.")
            col_name = fi.spec.column or fi.name
            assignments.append((col_name, Literal(value) if value is None else Param(value=value)))
        stmt = Update(
            table=self._table.name,
            assignments=tuple(assignments),
            where=self._where,
        )
        sql, params = self._builder.render(stmt)
        return await self._db._execute_dml(sql, params)

    def distinct(self) -> AsyncQuerySet[ET]:
        """Enable ``SELECT DISTINCT``."""
        q = self._clone()
        q._distinct = True
        return q

    def without_distinct(self) -> AsyncQuerySet[ET]:
        """Disable ``SELECT DISTINCT`` (reverses a previous :meth:`distinct` call)."""
        q = self._clone()
        q._distinct = False
        return q

    def for_update(self, *, skip_locked: bool = False) -> AsyncQuerySet[ET]:
        """Append ``FOR UPDATE [SKIP LOCKED]``."""
        q = self._clone()
        q._for_update = True
        q._for_update_skip_locked = skip_locked
        return q

    def page(self, pagenum: int, pagesize: int = 10) -> AsyncQuerySet[ET]:
        """Return a page of results (1-based page numbers)."""
        if pagenum < 1:
            raise ValueError("pagenum must be >= 1")
        return self.offset((pagenum - 1) * pagesize).limit(pagesize)

    def random(self, n: int) -> AsyncQuerySet[ET]:
        """Return *n* randomly ordered rows."""
        fname = "RAND" if self._db._provider == "mariadb" else "RANDOM"
        return self.order_by(OrderItem(FunctionCall(fname, ()))).limit(n)

    async def _aggregate(self, func: str, attr: str) -> Any:
        fi = self._entity_class._fields_.get(attr)
        if fi is None:
            raise ValueError(f"{self._entity_class.__name__!r} has no field {attr!r}.")
        col = fi.spec.column or fi.name
        stmt = Select(
            columns=(Alias(FunctionCall(func, (ColumnRef(col),)), "_agg"),),
            from_table=self._table.name,
            joins=self._joins,
            where=self._where,
        )
        sql, params = self._builder.render(stmt)
        rows = await self._db._execute(sql, params)
        return rows[0][0] if rows else None

    async def sum(self, attr: str) -> Any:
        """Return ``SUM(attr)`` or ``None`` when no rows match."""
        return await self._aggregate("SUM", attr)

    async def avg(self, attr: str) -> Any:
        """Return ``AVG(attr)`` or ``None`` when no rows match."""
        return await self._aggregate("AVG", attr)

    async def min(self, attr: str) -> Any:
        """Return ``MIN(attr)`` or ``None`` when no rows match."""
        return await self._aggregate("MIN", attr)

    async def max(self, attr: str) -> Any:
        """Return ``MAX(attr)`` or ``None`` when no rows match."""
        return await self._aggregate("MAX", attr)

    async def group_concat(self, attr: str, sep: str = ",") -> str | None:
        """Return the concatenation of all non-NULL values of *attr*.

        Uses ``GROUP_CONCAT(col, sep)`` on SQLite / MariaDB and
        ``STRING_AGG(col::text, sep)`` on PostgreSQL.
        Returns ``None`` when no rows match or all values are NULL.
        """
        fi = self._entity_class._fields_.get(attr)
        if fi is None:
            raise ValueError(f"{self._entity_class.__name__!r} has no field {attr!r}.")
        col = fi.spec.column or fi.name
        provider = self._db._provider

        where_parts: list[str] = []
        extra_params: list[Any] = []
        if self._where is not None:
            where_sql, where_params = self._builder.render(
                Select(
                    columns=(Literal(1),),
                    from_table=self._table.name,
                    where=self._where,
                )
            )
            where_idx = where_sql.upper().find(" WHERE ")
            if where_idx != -1:  # pragma: no branch
                where_parts.append(where_sql[where_idx:])
                extra_params.extend(where_params)

        table = self._table.name
        where_clause = where_parts[0] if where_parts else ""  # pragma: no branch

        if provider == "postgres":  # pragma: no cover
            func_sql = f"SELECT STRING_AGG({col}::text, ?) FROM {table}{where_clause}"
        else:
            func_sql = f"SELECT GROUP_CONCAT({col}, ?) FROM {table}{where_clause}"

        params: list[Any] = [sep, *extra_params]
        rows = await self._db._execute(func_sql, params)
        return rows[0][0] if rows else None

    def get_sql(self) -> str:
        """Return the SQL string that :meth:`fetch_all` would execute."""
        stmt = self._build_select()
        sql, _ = self._builder.render(stmt)
        return sql

    async def show(self, width: int = 120, *, file: IO[str] | None = None) -> None:
        """Async counterpart of :meth:`~nextorm.query.QuerySet.show`.

        Fetches all rows asynchronously and renders them as a plain-text
        table to *file* (default: ``sys.stdout``).

        Parameters
        ----------
        width:
            Maximum total table width in characters.
        file:
            Output stream; defaults to ``sys.stdout``.
        """
        out = file or sys.stdout
        entity_cls = self._entity_class
        field_names = [fi.name for fi in entity_cls._fields_.values() if not fi.spec.lazy]
        if not field_names:
            print("(no columns)", file=out)
            return
        results = await self.fetch_all()
        if not results:
            print("(no results)", file=out)
            return
        rows: list[list[str]] = [[str(getattr(r, name, "")) for name in field_names] for r in results]
        col_widths = [
            max(len(h), max((len(row[i]) for row in rows), default=0))
            for i, h in enumerate(field_names)
        ]
        total = 1 + sum(w + 3 for w in col_widths)
        if total > width and field_names:
            budget = max(1, (width - len(field_names) * 3 - 1) // len(field_names))
            col_widths = [min(w, budget) for w in col_widths]
            rows = [[cell[:cw] for cell, cw in zip(row, col_widths, strict=False)] for row in rows]
        sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
        hdr = (
            "|" + "|".join(f" {h:<{w}} " for h, w in zip(field_names, col_widths, strict=False)) + "|"
        )
        print(sep, file=out)
        print(hdr, file=out)
        print(sep, file=out)
        for row in rows:
            line = "|" + "|".join(f" {c:<{w}} " for c, w in zip(row, col_widths, strict=False)) + "|"
            print(line, file=out)
        print(sep, file=out)

    async def _do_async_prefetch(self, results: list[ET]) -> None:
        """Execute async prefetch queries and attach results to *results*."""
        if not results:
            return
        entity_cls = self._entity_class
        pk_fields = entity_cls._pk_fields_
        if not pk_fields:
            return

        from nextorm.database import _get_pk_val  # noqa: PLC0415

        owner_pks = [_get_pk_val(obj) for obj in results]
        owner_by_pk: dict[Any, ET] = {
            _get_pk_val(obj): obj for obj in results if _get_pk_val(obj) is not None
        }
        ph = "?" if self._db._provider == "sqlite" else "%s"

        for rel_name in self._prefetches:
            ri = entity_cls._relations_.get(rel_name)
            if ri is None:
                raise ValueError(f"Entity {entity_cls.__name__!r} has no relation {rel_name!r}.")

            from nextorm.collection import RelatedCollection  # noqa: PLC0415
            from nextorm.fields import RelationKind  # noqa: PLC0415

            if ri.spec.kind != RelationKind.SET:
                # Batch-load Single (FK) relation
                from nextorm.entity import _resolve_entity_target  # noqa: PLC0415

                resolved_target = _resolve_entity_target(ri.spec.target)
                if resolved_target is None:
                    continue
                from typing import cast as _cast  # noqa: PLC0415

                target_cls = _cast("type[Entity]", resolved_target)
                target_pk_field = target_cls._pk_field_
                if target_pk_field is None:
                    continue
                fk_key = f"_{rel_name}_id"
                obj_key = f"_{rel_name}_obj"
                fk_ids = [vars(obj).get(fk_key) for obj in results]
                unique_fk_ids = list(dict.fromkeys(fk for fk in fk_ids if fk is not None))
                if not unique_fk_ids:
                    for obj in results:
                        vars(obj)[obj_key] = None
                    continue
                target_pk_col = target_cls._fields_[target_pk_field].spec.column or target_pk_field
                placeholders = ", ".join(ph for _ in unique_fk_ids)
                target_rows = await self._db._execute(
                    f"SELECT * FROM {target_cls._table_name_} "
                    f"WHERE {target_pk_col} IN ({placeholders})",
                    unique_fk_ids,
                )
                target_qs = self._db.aselect(target_cls)
                related_by_pk: dict[Any, Any] = {}
                for trow in target_rows:
                    tobj = target_qs._map_row(trow)
                    tpk = getattr(tobj, target_pk_field)
                    related_by_pk[tpk] = tobj
                for obj, fk_id in zip(results, fk_ids, strict=True):
                    vars(obj)[obj_key] = related_by_pk.get(fk_id) if fk_id is not None else None
                continue

            target = ri.spec.target
            from nextorm.entity import (  # noqa: PLC0415
                _matches_entity,
                _resolve_entity_target,
            )

            resolved = _resolve_entity_target(target)
            if resolved is None:
                continue
            from typing import cast as _cast2  # noqa: PLC0415

            target_cls = _cast2("type[Entity]", resolved)

            owner_cls = entity_cls
            is_m2m = any(
                r.spec.kind == RelationKind.SET and _matches_entity(r.spec.target, owner_cls)
                for r in target_cls._relations_.values()
            )

            if is_m2m:
                join_table = "_".join(sorted([owner_cls._table_name_, target_cls._table_name_]))
                owner_col = f"{owner_cls._table_name_}_id"
                inline = ", ".join(ph for _ in owner_pks)
                join_rows = await self._db._execute(
                    f"SELECT * FROM {join_table} WHERE {owner_col} IN ({inline})",
                    owner_pks,
                )
                target_pks_for: dict[Any, list[Any]] = {}
                for jrow in join_rows:
                    owner_pk_val = jrow[0] if jrow[0] in owner_by_pk else jrow[1]
                    target_pk_val = jrow[1] if owner_pk_val == jrow[0] else jrow[0]
                    target_pks_for.setdefault(owner_pk_val, []).append(target_pk_val)

                if not target_pks_for:
                    for obj in results:
                        col_obj: RelatedCollection[Any] = RelatedCollection(obj, ri, self._db)  # type: ignore[arg-type]
                        col_obj._cache = []
                        vars(obj)[f"_{rel_name}_col"] = col_obj
                    continue

                all_target_pks = [pk for pks in target_pks_for.values() for pk in pks]
                target_pk_field = target_cls._pk_field_
                assert target_pk_field is not None
                target_pk_col = target_cls._fields_[target_pk_field].spec.column or target_pk_field
                all_inline = ", ".join(ph for _ in all_target_pks)
                target_rows = await self._db._execute(
                    f"SELECT * FROM {target_cls._table_name_} "
                    f"WHERE {target_pk_col} IN ({all_inline})",
                    all_target_pks,
                )
                target_qs = self._db.aselect(target_cls)
                target_by_pk: dict[Any, Any] = {}
                for trow in target_rows:
                    tobj = target_qs._map_row(trow)
                    tpk = getattr(tobj, target_pk_field)
                    target_by_pk[tpk] = tobj

                for obj in results:
                    opk = _get_pk_val(obj)
                    related_objs = [
                        target_by_pk[tpk]
                        for tpk in target_pks_for.get(opk, [])
                        if tpk in target_by_pk
                    ]
                    col: RelatedCollection[Any] = RelatedCollection(obj, ri, self._db)  # type: ignore[arg-type]
                    col._cache = related_objs
                    vars(obj)[f"_{rel_name}_col"] = col
            else:
                # O2M — find back-ref FK column on target
                back_ref = next(
                    (
                        r
                        for r in target_cls._relations_.values()
                        if r.spec.kind == RelationKind.SINGLE
                        and _matches_entity(r.spec.target, entity_cls)
                    ),
                    None,
                )
                if back_ref is None:
                    continue
                fk_col = f"{back_ref.name}_id"
                inline = ", ".join(ph for _ in owner_pks)
                target_rows = await self._db._execute(
                    f"SELECT * FROM {target_cls._table_name_} WHERE {fk_col} IN ({inline})",
                    owner_pks,
                )
                target_qs = self._db.aselect(target_cls)
                grouped: dict[Any, list[Any]] = {pk: [] for pk in owner_pks}
                for trow in target_rows:
                    tobj = target_qs._map_row(trow)
                    fk_val = vars(tobj).get(f"_{back_ref.name}_id")
                    if fk_val in grouped:  # pragma: no branch
                        grouped[fk_val].append(tobj)

                for obj in results:
                    opk = _get_pk_val(obj)
                    col2: RelatedCollection[Any] = RelatedCollection(obj, ri, self._db)  # type: ignore[arg-type]
                    col2._cache = grouped.get(opk, [])
                    vars(obj)[f"_{rel_name}_col"] = col2

    async def raw(self, sql: str, params: list[Any] | None = None) -> list[ET]:
        """Execute *sql* and map each result row to an entity instance.

        Column names in the cursor description are matched to entity fields
        by name.  Columns that don't match any field are silently ignored.
        """
        from nextorm.query import _build_column_map_from_names  # noqa: PLC0415

        rows, col_names = await self._db._execute_described(sql, params or [])
        col_map = _build_column_map_from_names(self._entity_class, col_names)
        return [self._map_raw_row(row, col_map) for row in rows]

    async def raw_one(self, sql: str, params: list[Any] | None = None) -> ET | None:
        """Execute *sql* and return the first mapped entity, or ``None``."""
        from nextorm.query import _build_column_map_from_names  # noqa: PLC0415

        rows, col_names = await self._db._execute_described(sql, params or [])
        if not rows:
            return None
        col_map = _build_column_map_from_names(self._entity_class, col_names)
        return self._map_raw_row(rows[0], col_map)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_select(self, *, extra_limit: int | None = None) -> Select:
        lim = self._lim
        if extra_limit is not None:
            lim = extra_limit if self._lim is None else min(self._lim, extra_limit)
        # When JOINs are present, qualify each column ref with the main table name to
        # avoid "ambiguous column name" errors (multiple joined tables may share 'id').
        if self._explicit_columns is not None:
            if self._joins:
                tname = self._table.name
                columns: tuple[Any, ...] = tuple(
                    ColumnRef(c.column, tname) if isinstance(c, ColumnRef) and c.table is None else c  # pyright: ignore[reportUnnecessaryIsInstance]
                    for c in self._explicit_columns
                )
            else:
                columns = self._explicit_columns
        elif self._joins:
            columns = (Star(self._table.name),)
        else:
            columns = (Star(),)
        return Select(
            columns=columns,
            from_table=self._table.name,
            joins=self._joins,
            where=self._where,
            order_by=self._order,
            limit=lim,
            offset=self._off,
            distinct=self._distinct,
            for_update=self._for_update or self._for_update_skip_locked,
            for_update_skip_locked=self._for_update_skip_locked,
        )

    def _map_raw_row(self, row: tuple[Any, ...], col_map: list[str | None]) -> ET:
        """Hydrate *row* using an explicit *col_map* (no identity-map caching)."""
        obj: ET = object.__new__(self._entity_class)
        vars(obj)["_db_"] = self._db
        for field_name, value in zip(col_map, row, strict=False):
            if field_name is None:
                continue
            if field_name.startswith("_") and field_name.endswith("_id"):
                vars(obj)[field_name] = value
            else:
                setattr(obj, field_name, value)
        for lname in self._lazy_field_names:
            vars(obj)[f"_field_{lname}"] = _LAZY_SENTINEL
        obj.after_load()
        return obj

    def _map_row(self, row: tuple[Any, ...]) -> ET:
        entity_cls = self._entity_class
        obj: ET = object.__new__(entity_cls)
        vars(obj)["_db_"] = self._db
        # Intermediate storage for composite FK components keyed by fk_key.
        _cfk_builders: dict[str, list[Any]] = {}
        for field_name, value in zip(self._column_map, row, strict=False):
            if field_name is None:  # pragma: no cover
                continue
            if "\x1f" in field_name:
                # Composite FK component: "_shop_order_id\x1f0\x1f2"
                fk_key, idx_str, total_str = field_name.split("\x1f")
                idx, total = int(idx_str), int(total_str)
                if fk_key not in _cfk_builders:
                    _cfk_builders[fk_key] = [None] * total
                _cfk_builders[fk_key][idx] = value
            elif field_name.startswith("_") and field_name.endswith("_id"):
                vars(obj)[field_name] = value
            else:
                setattr(obj, field_name, value)
        # Assemble composite FK tuples
        for fk_key, components in _cfk_builders.items():
            vars(obj)[fk_key] = tuple(components) if any(c is not None for c in components) else None
        for lname in self._lazy_field_names:
            vars(obj)[f"_field_{lname}"] = _LAZY_SENTINEL
        obj.after_load()
        # Option A — per-field optimistic tracking (mirrors sync QuerySet._map_row).
        if entity_cls._pk_fields_:
            dbvals: dict[str, Any] = {}
            for fi in entity_cls._fields_.values():
                if not fi.spec.primary_key and not fi.spec.volatile and not fi.spec.lazy:
                    col = fi.spec.column or fi.name
                    dbvals[col] = getattr(obj, fi.name, None)
            table_col_names = self._table.column_names()
            for ri in entity_cls._relations_.values():
                if ri.spec.kind == RelationKind.SINGLE:
                    fk_val = vars(obj).get(f"_{ri.name}_id")
                    if isinstance(fk_val, tuple):
                        from nextorm.entity import (
                            _derive_composite_fk_cols,  # noqa: PLC0415
                        )

                        fk_col_names = ri.spec.columns or _derive_composite_fk_cols(
                            ri.name, ri.spec.target
                        )
                        _fk_tuple3 = cast("tuple[Any, ...]", fk_val)  # type: ignore[redundant-cast]
                        for col_name, val in zip(fk_col_names, _fk_tuple3, strict=False):
                            if col_name in table_col_names:  # pragma: no branch
                                dbvals[col_name] = val
                    else:
                        fk_col = (
                            (ri.spec.columns[0] if ri.spec.columns else None)
                            or ri.spec.column
                            or f"{ri.name}_id"
                        )
                        if fk_col not in table_col_names:
                            continue
                        dbvals[fk_col] = fk_val
            vars(obj)["_dbvals_"] = dbvals
            vars(obj)["_read_cols_"] = set()
        return obj
