Internals — How It Works
========================

This chapter describes the design and implementation of NextORM for contributors
and developers who want to understand the engine.

Entity metaclass (``EntityMeta``)
----------------------------------

When Python processes a class statement like:

.. code-block:: python

   class Product(Entity):
       name:  Req[str]
       price: Req[float]
       tags:  Set["Tag"]

the metaclass :class:`~nextorm.entity.EntityMeta` intercepts ``type.__new__``
and walks the class annotations.  For each annotation whose
``__origin__`` is one of the NextORM field markers (``PK``, ``Req``, ``Opt``,
``Local``, ``Set``, ``Single``) it:

1. Creates a **descriptor** (``FieldDescriptor``, ``SetDescriptor``,
   ``SingleDescriptor``, or ``LocalDescriptor``) and replaces the annotation
   placeholder on the class.
2. Records metadata in the class-level dicts ``_fields_``, ``_relations_``,
   and ``_locals_``.
3. Registers the entity in the global ``_entity_registry`` so
   :class:`~nextorm.database.Database` can discover it without explicit
   ``register()`` calls.

Type checking vs. runtime
~~~~~~~~~~~~~~~~~~~~~~~~~

The ``PK``, ``Req``, ``Opt``, ``Set``, and ``Single`` markers are real Python
generic classes with custom ``__get__`` / ``__set__``.  Type checkers (pyright,
mypy) see them as typed descriptors — ``PK[int].__get__`` returns ``ColumnExpr``
at the class level and ``int`` at the instance level.  At runtime, however,
``EntityMeta.__new__`` *replaces* each annotated attribute with a real
descriptor object, so the ``__get__`` / ``__set__`` on the marker classes are
never called.

Identity map and session cache
------------------------------

:class:`~nextorm.session.SessionCache` is a lightweight in-process object store:

- ``_objects`` — ``dict[(EntityClass, pk) → entity_instance]``
- ``_to_save`` — ordered list of new (unsaved) entity instances
- ``_dirty`` — set of modified entity instances

When an entity attribute is written via ``FieldDescriptor.__set__``, the
descriptor calls ``cache.mark_dirty(instance)`` on the current session.  On
session exit (or explicit :func:`~nextorm.__init__.flush`) all dirty and
pending-save entities are written through :meth:`~nextorm.database.Database.save`.

The session stack is stored in a :class:`threading.local` variable for sync code
and a :class:`~contextvars.ContextVar` for async code. The
:class:`~nextorm.session.DBSessionManager` reads whichever stack is active.

Database and provider abstraction
----------------------------------

:class:`~nextorm.database.Database` owns a single persistent connection
(or a :class:`~nextorm.pool.ConnectionPool`) and a reference to a
:class:`~nextorm.providers.SyncProvider`.  The provider handles all SQL
dialect variations:

.. code-block:: text

   Database ─── SyncProvider ─── sqlite3 / psycopg / PyMySQL
                           └──── DDLRenderer (SQLiteRenderer / PostgresRenderer / MariaDBRenderer)

:class:`~nextorm.async_database.AsyncDatabase` is structurally identical but
inherits the provider from the async provider registry and uses
``aiosqlite`` / ``psycopg[async]`` / ``asyncmy``.

Schema layer
------------

The schema layer (``nextorm.schema``) produces :class:`~nextorm.schema.core.Table`
objects from entity metadata.  ``build_schema(entities)`` walks each entity's
``_fields_`` and ``_relations_`` and produces the full table+column+FK graph.

``DDLRenderer`` serialises this graph to SQL ``CREATE TABLE`` statements.
``diff_schemas`` compares two serialised schema dicts and emits ``ALTER TABLE``
statements — this is what ``makemigrations`` uses to auto-generate migration files.

Query compilation
-----------------

A :class:`~nextorm.query.QuerySet` builds an AST of immutable SQL node objects
from ``nextorm.sql.nodes``:

.. code-block:: text

   QuerySet
   ├── _entity_class  → provides column names
   ├── _where         → BinOp (AST tree)
   ├── _order         → tuple[OrderItem]
   ├── _joins         → tuple[(join_type, table, alias, BinOp)]
   └── _lim / _off    → int | None

When a terminal method (``fetch_all()``, ``count()``, …) is called, the
:class:`~nextorm.sql.builder.SQLBuilder` renders the AST to a parameterised
SQL string and hands it to ``Database._execute()``.

Each ``QuerySet`` method returns a shallow *clone* so the original is never
mutated — the builder pattern is fully immutable.

Generator-expression decompiler
---------------------------------

The :func:`~nextorm.generators.select` function accepts a generator expression
and decompiles its bytecode back to a filter predicate.  It uses
:mod:`dis` to walk the bytecode instructions and map comparison
operations (``COMPARE_OP``, ``BINARY_OP``) to :class:`~nextorm.sql.nodes.BinOp`
AST nodes.

Because bytecode layouts differ between Python versions, the decompiler
normalises opcode names per-version.  Complex Python expressions (function
calls, multi-level attribute chains) raise
:exc:`~nextorm.generators.DecompileError` — the filter-based API handles those
cases.

Flush and commit pipeline
--------------------------

The session-exit path implements a PonyORM-style staged commit:

.. code-block:: text

   Session.__exit__
   │
   ├── [clean exit]
   │   ├── _collect_dbs()   ← scans _objects + _to_save + _dirty
   │   ├── for db in dbs: db.flush()    ← write all pending changes
   │   │   └── on failure → rollback all, re-raise
   │   ├── primary._commit_transaction()
   │   │   └── on failure → rollback secondaries, re-raise
   │   └── for db in secondaries: db._commit_transaction()
   │       └── each failure appended; first one re-raised at end
   │
   └── [exception]
       └── for db in dbs: db._rollback_transaction()  (errors swallowed)

``_collect_dbs`` discovers database instances by inspecting the ``_db_``
instance variable of every entity in the session cache (identity map, pending
save queue, and dirty set).

Optimistic concurrency
-----------------------

When ``optimistic=True`` (the default), NextORM tracks which columns were
*read* since the entity was loaded (stored in ``instance.__dict__["_read_cols_"]``).
On ``UPDATE``, it appends ``AND col = original_value`` for each read column.
If the row was changed by another transaction in the interim, the update
matches zero rows and :exc:`~nextorm.exceptions.OptimisticCheckError` is raised.

This implements per-field optimistic concurrency checking, matching PonyORM's
semantics.

Single-table inheritance
-------------------------

STI entities share one database table.  ``EntityMeta`` looks for
``_discriminator_col_`` on the parent class and ``_discriminator_val_`` on each
subclass.

- **SELECT** — a ``WHERE kind = 'dog'`` clause is automatically appended when
  querying a subclass, and the correct Python class is chosen when loading rows
  from the parent class based on the discriminator value.
- **INSERT** — the discriminator column is included in the INSERT with the
  subclass's configured value.
- **Schema** — the parent class owns the table; subclass columns are merged in.

Provider system
---------------

Providers are registered in a global registry keyed by name.  You can register
custom providers:

.. code-block:: python

   from nextorm.providers import register_provider
   from mylib.provider import MySyncProvider

   register_provider("mydb", MySyncProvider)

A :class:`~nextorm.providers.SyncProvider` must implement:

.. code-block:: python

   class SyncProvider:
       def connect(self, *args, **kwargs) -> SyncConnection: ...
       def last_insert_id(self, cursor: SyncCursor) -> int | None: ...
       def sql_builder_class(self) -> type[SQLBuilder]: ...
       def ddl_renderer_class(self) -> type[DDLRenderer]: ...
       def paramstyle(self) -> str: ...   # "qmark" | "format" | "pyformat"

Connection lifecycle
--------------------

Without pooling, :class:`~nextorm.database.Database` maintains one persistent
connection opened at first use (via ``_ensure_connection``).  When pooling is
enabled, each ``_execute`` / ``_execute_dml`` cycle checks out a connection,
runs the SQL, and immediately returns it.

The same model applies to :class:`~nextorm.async_database.AsyncDatabase` using
:class:`~nextorm.pool.AsyncConnectionPool`.
