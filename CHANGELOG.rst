Changelog
=========

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

**New features:**

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
