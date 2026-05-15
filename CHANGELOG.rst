Changelog
=========

0.3.0 — not released yet
------------------------

**New features:**

- ``RelatedCollection.remove()`` now accepts an iterable (e.g. a list) of
  items in addition to individual positional arguments:

  .. code-block:: python

     # Remove items one by one (unchanged)
     post.comments.remove(c1, c2)

     # Remove a pre-collected list of items (new)
     comments_to_remove = [c for c in post.comments if c.spam]
     post.comments.remove(comments_to_remove)

  Works for both one-to-many and many-to-many relations.

- ``QuerySet.order_by()`` and ``AsyncQuerySet.order_by()`` now accept two
  additional call forms in addition to the existing ``OrderItem`` form:

  * **Bare** :class:`~nextorm.expr.ColumnExpr` — auto-wrapped as ``ASC``::

       db.select(User).order_by(User.name)

  * **Lambda** — receives an :class:`~nextorm.query.EntityProxy` and returns a
    column expression, an ordering item, or a tuple of those::

       db.select(User).order_by(lambda u: u.name)                       # ASC
       db.select(User).order_by(lambda u: u.name.desc())                # DESC
       db.select(User).order_by(lambda u: (u.age.desc(), u.name.asc())) # multi

  * **Chained relation traversal in lambdas** — the lambda proxy now supports
    multi-level attribute access across ``Single`` relations; the required
    ``JOIN`` clauses are emitted automatically and de-duplicated::

       # Order by a field three hops away
       db.select(CartItem).order_by(
           lambda ci: (
               ci.product_variation.product.shop.slug,
               ci.product_variation.product.name,
               ci.product_variation.sku,
           )
       )

- ``Entity.select()`` accepts an optional lambda predicate as the first
  positional argument and/or keyword-argument equality filters:

  .. code-block:: python

     active_users = User.select(lambda u: u.active).fetch_all()
     admins = User.select(role="admin").fetch_all()
     admin_users = User.select(lambda u: u.active, role="admin").fetch_all()

- Multi-level relation traversal in generator-expression queries and
  lambda predicates now supports chains of arbitrary depth (no hard limit).
  Each intermediate relation generates a SQL ``JOIN`` automatically:

  .. code-block:: python

     items = select(i for i in Item if i.brand.category.slug == "tools")
     items = Item.select(lambda i: i.brand.category.slug == "tools").fetch_all()

- New table management methods for database and entity cleanup:

  - ``Database.create_tables()`` — create all entity tables if they don't exist
  - ``Database.drop_table(name, if_exists=True, with_all_data=True)`` —
    drop a specific table with optional data safety checks
  - ``Database.drop_all_tables(with_all_data=True)`` — drop all database tables
  - ``Entity.drop_table(with_all_data=True)`` — drop the table for a single entity
  - ``RelatedCollection.drop_table(with_all_data=True)`` — drop M2M join table

  Example::

    # Useful for test cleanup
    db.drop_all_tables(with_all_data=True)

- ``Database.disconnect()`` — close all connections in the pool (useful before
  deleting SQLite files)

- :class:`~nextorm.debug.capture_sql` — context manager that records all SQL
  statements executed within its scope into a list of
  :class:`~nextorm.debug.CapturedQuery` objects (each with ``.sql`` and
  ``.params``).  Designed for test assertions without mocking::

     from nextorm import capture_sql

     with capture_sql() as queries:
         db.select(User).fetch_all()
     assert len(queries) == 1
     assert queries[0].params == []

  Nesting is supported; only the innermost context captures.

- :class:`~nextorm.debug.async_capture_sql` — async counterpart of
  :class:`~nextorm.debug.capture_sql`, backed by a
  :class:`contextvars.ContextVar` so it works across ``await`` boundaries::

     from nextorm import async_capture_sql

     async with async_capture_sql() as queries:
         await db.aselect(User).fetch_all()
     assert len(queries) == 1

- ``AsyncQuerySet.first()`` — async alias for :meth:`~nextorm.async_database.AsyncQuerySet.fetch_one`.

- ``AsyncQuerySet.prefetch(*relation_attrs)`` — async counterpart of
  :meth:`~nextorm.query.QuerySet.prefetch`; issues one batch query per relation
  after the main ``SELECT`` to avoid N+1 queries.

- ``AsyncQuerySet.show()`` — renamed from :meth:`~nextorm.async_database.AsyncQuerySet.ashow`;
  fetches all rows and renders them as a plain-text table to stdout.

- ``AsyncDatabase.disconnect()`` — async counterpart of
  :meth:`~nextorm.database.Database.disconnect`; closes the underlying async
  connection.

- ``AsyncDatabase.get_ddl()`` — async counterpart of
  :meth:`~nextorm.database.Database.get_ddl`; returns the DDL statements
  captured during :meth:`~nextorm.async_database.AsyncDatabase.generate_mapping`.

- ``AsyncDatabase.migrate()`` — async counterpart of
  :meth:`~nextorm.database.Database.migrate`; introspects the live schema,
  computes the diff, and executes pending DDL statements.

- ``AsyncDatabase.unbind()`` — async counterpart of
  :meth:`~nextorm.database.Database.unbind`; closes the connection and clears
  the provider binding.

- ``QuerySet.filter()`` and ``AsyncQuerySet.filter()`` now accept keyword
  arguments as field-equality shortcuts, combined with ``AND``:

  .. code-block:: python

     db.select(User).filter(active=True).fetch_all()
     db.select(User).filter(User.age >= 18, active=True).fetch_all()

- ``AsyncQuerySet.where()`` upgraded to use bytecode decompilation first
  (matching ``QuerySet.where()``), with a proxy-based fallback.  M2M
  containment checks and boolean attribute predicates now work in async
  queries too.

- Unified query API — all entry-point methods now share the same calling
  convention: ``method(predicate=None, *conditions: SqlNode, **kwargs)``.
  The following methods gained ``*conditions`` (positional ``SqlNode``) and
  updated return types:

  - ``Entity.select(predicate=None, *conditions, **kwargs) -> QuerySet[Self]``
  - ``Entity.aselect(predicate=None, *conditions, **kwargs) -> AsyncQuerySet[Self]``
  - ``Entity.get(predicate=None, *conditions, **kwargs) -> Self | None``
  - ``Entity.aget(predicate=None, *conditions, **kwargs) -> Self | None``
  - ``Entity.exists(predicate=None, *conditions, **kwargs) -> bool``

  All arguments are optional and freely combinable:

  .. code-block:: python

     # Any combination is valid:
     User.select()                                        # whole table
     User.select(User.age >= 18)                          # SqlNode condition
     User.select(lambda u: u.active, role="admin")        # predicate + kwargs
     User.get(lambda u: u.age > 18, User.active == True)  # predicate + condition
     User.exists(active=True)                             # kwargs only

- ``RelatedCollection`` API redesigned for consistency with
  :class:`~nextorm.query.QuerySet`:

  * ``select(predicate=None, *conditions, **kwargs) -> QuerySet[T]`` — now a
    unified entry point; no args returns the whole collection.
  * ``where(lambda c: ...)`` — lambda-predicate filtering.
  * ``filter(*conditions, **kwargs)`` — ``SqlNode`` conditions and kwargs.

  .. code-block:: python

     # Get the whole collection as a QuerySet
     post.comments.select().order_by(Comment.created_at.asc()).fetch_all()

     # Filter with SqlNode or kwargs
     post.comments.filter(Comment.approved == True).fetch_all()
     post.comments.filter(approved=True).fetch_all()

     # Filter with a lambda
     post.comments.where(lambda c: c.score > 5).fetch_all()

     # All combined in select()
     post.comments.select(
         lambda c: c.score > 5,
         Comment.approved == True,
         spam=False,
     ).fetch_all()

**Bug fixes:**

- Fixed ``QuerySet.__getitem__`` (slice form) to preserve any ``.offset()`` /
  ``.limit()`` already set on the queryset when a bare ``[:]`` slice is used.
  Previously ``qs.offset(n).limit(m)[:]`` silently reset the offset to 0.

- Fixed ``QuerySet._build_select()`` (and its async counterpart
  ``AsyncQuerySet._build_select()``) to qualify all selected columns with the
  main table name when one or more ``JOIN`` clauses are present.  Without this
  fix, ``SELECT *`` or explicit column lists produced ``ambiguous column name``
  errors from SQLite whenever a ``JOIN`` caused two tables to expose the same
  column name (most commonly ``id``).

- ``Opt[str]`` and ``Opt[LongStr]`` fields without an explicit ``nullable=True``
  now correctly default to ``""`` (empty string) rather than ``None``.  The
  zero-value is injected at the ``FieldSpec`` level so that
  ``Entity.__init__`` never stores ``None`` for a non-nullable string field.

- ``Database.delete_instance()`` no longer raises ``RuntimeError`` when a
  ``Set[T]`` relation targets an entity type that belongs to a *different*
  database scope.  The cascade loop now skips entities whose table is absent
  from the current database's schema.

- ``Database.execute()`` and ``AsyncDatabase.execute()`` now flush all pending
  inserts and dirty objects in the current session before executing the raw SQL
  statement, ensuring consistency with the auto-flush semantics of
  ``QuerySet.fetch_all()`` / ``get()`` / ``exists()`` etc.

- ``Database._execute()`` and ``AsyncDatabase._execute()`` now auto-flushes pending session objects before
  executing any SELECT query.

- Fixed entity persistence for user-assigned (non-auto-increment) primary keys.
  Entities with non-auto single-field or composite primary keys are now
  correctly inserted on first save instead of silently failing with UPDATE.
  The entity identity map now properly tracks whether an entity is new
  (not yet in the database) by checking ``_dbvals_`` presence.

- Fixed ``PK[int]`` marker to respect explicit ``auto=False`` option. Previously,
  integer primary keys always defaulted to auto-increment regardless of user
  options. Now users can define ``id: PK[int] = PK(auto=False)`` for
  user-assigned integer primary keys.

- ``SQLiteSyncProvider`` and ``SQLiteAsyncProvider`` now accept ``filename``
  as a keyword argument alias for ``database``, matching Pony ORM's
  ``db.bind("sqlite", filename=":memory:")`` calling convention.

**Docs:**

- Add *Table management* section in ``docs/entities.rst`` covering
  ``Entity.drop_table()``, ``Database.drop_table()``,
  ``Database.drop_all_tables()``, ``Database.create_tables()``,
  ``RelatedCollection.drop_table()`` and ``Database.disconnect()``.
- Add *SQL capture* sections in ``docs/debugging.rst`` covering
  ``capture_sql``, ``CapturedQuery``, and ``async_capture_sql``.
- Expose all new ``Database`` methods in the API reference autosummary
  (``docs/api/database.rst``).
- Update ``docs/relations.rst`` to document ``RelatedCollection.select()``
  and ``RelatedCollection.filter()`` keyword-argument and predicate support.
- Update ``AGENTS.md`` quick-start loop to explicitly require updating narrative
  docs under ``docs/`` for any API change.

0.2.1 — 2026-05-03
-------------------

**New features:**

- Add ``Entity.flush()`` and ``Entity.commit()`` — convenience helpers
  to persist a single entity or persist and commit it
- ``Entity.get()``, ``Entity.exists()``, and ``Entity.aget()`` now accept
  an optional lambda predicate as the first positional argument, using the
  same bytecode decompiler as generator-expression queries.  Closure
  variables are resolved automatically:

  .. code-block:: python

     user = User.get(lambda u: u.age >= 18)
     min_age = 18
     user = User.get(lambda u: u.age >= min_age, role="admin")
     user = await User.aget(lambda u: u.email == email)

**Tests:**

- Add unit tests for ``Entity.flush()`` and ``Entity.commit()``

**Docs & tooling:**

- Add documentation for Composite constraints
- Fix docstrings for field markers
- Document per-entity flush/commit usage in the entities guide.
- Add developer tooling: `.github/skills/fix-type-leak` to help triage
  and fix type errors leaking into consumer projects

0.2.0 — 2026-05-02
-------------------

**Breaking API changes:**

- **Field and relation declarations now use marker-call syntax.**
  ``FieldSpec(...)`` and ``RelationSpec(...)`` as class-body values are removed.
  Use the marker itself as the callable instead:

  .. code-block:: python

     # Before (v0.1 — removed)
     class Account(Entity):
         id:      PK[int]      = FieldSpec(auto=True)
         name:    Req[str]     = FieldSpec(max_len=128)
         notes:   Opt[str]     = FieldSpec(nullable=True)
         owner:   Single[User] = RelationSpec(column="owner_id")
         items:   Set[Item]    = RelationSpec(reverse="account")

     # After (v0.2 — new syntax)
     class Account(Entity):
         id:      PK[int]      = PK(auto=True)
         name:    Req[str]     = Req(128)
         notes:   Opt[str]     = Opt(nullable=True)
         owner:   Single[User] = Single(column="owner_id")
         items:   Set[Item]    = Set(reverse="account")

- **Removed ``async_db_session`` alias.** Use :func:`~nextorm.session.db_session`
  for both sync and async contexts — it is identical.

- **Positional argument shorthands** for scalar field markers.  The most common
  type-specific option can now be passed as the first (or second) positional
  argument instead of a keyword:

  .. list-table::
     :header-rows: 1
     :widths: 35 30 35

     * - Full keyword form
       - Positional shorthand
       - Positional argument(s)
     * - ``Req[str](max_len=128)``
       - ``Req[str](128)``
       - ``max_len``
     * - ``Opt[str](max_len=64)``
       - ``Opt[str](64)``
       - ``max_len``
     * - ``Req[int](size=32)``
       - ``Req[int](32)``
       - ``size``
     * - ``Req[float](tolerance=0.01)``
       - ``Req[float](0.01)``
       - ``tolerance``
     * - ``Req[Decimal](precision=10, scale=2)``
       - ``Req[Decimal](10, 2)``
       - ``precision``, ``scale``
     * - ``Req[datetime](precision=3)``
       - ``Req[datetime](3)``
       - ``precision``
     * - ``Req[Vec](dimensions=384)``
       - ``Req[Vec](384)``
       - ``dimensions``
     * - ``Req[uuid7](uuid_auto="v7")``
       - ``Req[uuid7]("v7")``
       - ``uuid_auto``

- **Local fields** (``Local[T]``) — transient fields that are never written to
  or read from the database.  Useful for computed properties, caches, and
  in-memory state.  Supports ``default`` and ``py_check`` options.

- **Enhanced relation options** — customize FK column names, join table names,
  cascade behaviour, and one-to-one ownership via keyword arguments on
  ``Single(...)`` and ``Set(...)``.

- **Comprehensive documentation** — added guides for Local fields, relation
  customization, and a complete option reference for all field markers.

0.1.4 — 2026-04-16
-------------------

- Set ``validate_relations`` to ``True`` by default.
- Removed documentation of auto-recovery and optional back-references.
- Use ``pdm`` for publishing in the CI workflow.
- README adjustments.

0.1.3 — 2026-04-15
-------------------

- Dynamic app version handling (no hardcoded version in ``pyproject.toml``
  and ``docs/conf.py``).
- Added NextORM icon and logos in dark, light, and neutral variants.

0.1.2 — 2026-04-13
-------------------

Initial release of NextORM — a modern Python ORM with async support, full type
annotations, and a generator-expression query DSL.

**Features:**

- Type-annotated fields: ``PK[int]``, ``Req[str]``, ``Opt[str]``, ``Set[T]``, ``Single[T]``
- Auto-save sessions — create entities inside ``db_session`` and they are
  committed automatically
- PonyORM-compatible DSL — generator-expression queries, ``Entity[pk]``,
  ``Entity.get()``, lifecycle hooks
- Full async support — ``AsyncDatabase``, ``await db.aselect(...)``,
  ``Entity.aselect()``, ``Entity.aget()``
- Built-in migrations CLI — ``nextorm makemigrations`` / ``nextorm migrate`` /
  ``nextorm showmigrations``
- Three providers — SQLite, PostgreSQL (psycopg3), MariaDB
- 100% branch coverage enforced in CI
