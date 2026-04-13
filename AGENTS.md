# Agent Instructions for NextORM

NextORM is a modern Python ORM with async support, full type annotations, and a
PonyORM-inspired query DSL. Minimum Python version: **3.12**.

---

## Quick-Start Agent Loop

1. **Before editing** — run `pdm test` and `pdm lint` to see the baseline state.
2. **After editing** — run `pdm run fix`, `pdm format`, `pdm typecheck`, `pdm coverage`.
3. **For API changes** — update or add docstrings; rebuild docs with `pdm docs-html`.
4. **Quality gate** — all tests must pass with **100% branch coverage** before work
   is considered complete.

---

## PDM Scripts

| Script | Command | Purpose |
|---|---|---|
| `pdm test` | `pytest` | Run the full test suite |
| `pdm coverage` | `pytest --cov=nextorm --cov-report=term-missing` | Tests + branch coverage report |
| `pdm typecheck` | `pyright` then `mypy` | Run **both** type checkers |
| `pdm pyright` | `pyright` | Pyright only |
| `pdm mypy` | `mypy` | mypy only |
| `pdm lint` | `ruff check .` | Ruff linter |
| `pdm fix` | `ruff check --fix .` | Auto-fix linting issues |
| `pdm format` | `ruff format .` | Format code |
| `pdm docs-html` | `sphinx-build -b html docs/ docs/_build/html` | Build Sphinx docs |
| `pdm docs-clean` | `rm -rf docs/_build` | Remove built docs |

Always run `pdm run fix` and `pdm format` before `pdm typecheck` — the formatter may change lines
that produce spurious type errors when unformatted.

---

## Project Structure

```
nextorm/              Source package
  __init__.py         Public API re-exports (the canonical import surface)
  entity.py           EntityMeta metaclass + Entity base class
  database.py         Sync Database class
  async_database.py   AsyncDatabase + AsyncQuerySet
  query.py            QuerySet (sync)
  fields.py           Field markers: PK, Req, Opt, Set, Single, Local, FieldSpec, …
  session.py          db_session context manager / decorator, flush/commit/rollback
  collection.py       RelatedCollection (lazy + prefetch collections)
  generators.py       Generator-expression query front-end (select/avg/sum/…)
  expr.py             ColumnExpr descriptor for column-level query nodes
  sql/                SQL AST nodes + builder (SQLiteBuilder, PostgresBuilder, …)
  schema/             DDL renderer + schema introspection
  migrations/         File-based migration runner + CLI
  providers/          Provider abstraction (SyncProvider, AsyncProvider, …)
  pool.py             ConnectionPool / AsyncConnectionPool
  debug.py            set_sql_debug, sql_debugging, QueryStat, global_stats
  exceptions.py       All public exception classes

tests/                One file per feature area
stubs/                Type stubs for third-party packages that lack them
docs/                 Sphinx documentation (Furo theme)
  conf.py             Sphinx config
  *.rst               User-guide chapters
  api/                API reference pages (autodoc)
  _build/html/        Generated HTML (gitignored)
```

---

## Architecture Notes

### Entity registration

`EntityMeta.__new__` adds every `Entity` subclass to a module-level
`_entity_registry` set at class-definition time (i.e. on import).
`Database(entities=[...])` or `db.register(...)` scopes a database to a subset.
`_find_db_for_entity(cls)` walks `_database_registry` to locate the right db.

### Session / dirty tracking

`db_session` (context manager or decorator) keeps a thread-local + `ContextVar`
stack in `_SessionStack`. Field descriptors record attribute accesses in
`_read_cols_` and mutations in `_dirty_cols_` on the entity instance.
On commit, only changed columns appear in `UPDATE` statements; only accessed
columns are checked by optimistic concurrency.

### Query compilation

`db.select(Entity)` returns a `QuerySet[Entity]`.  Chained methods (`.filter`,
`.order_by`, `.limit`, …) clone the `QuerySet` without mutating it.  Terminal
methods (`fetch_all`, `get`, `count`, …) compile the accumulated AST nodes
into SQL via the provider-specific `SQLBuilder`.

Generator-expression queries (`select(p for p in Product if p.price > 0)`)
are compiled by bytecode decompilation in `generators.py`.

### Async

`AsyncDatabase.aselect(Entity)` returns `AsyncQuerySet[Entity]`.  Terminal
methods are coroutines (`await qs.fetch_all()`).  Everything else (`.filter`,
`.order_by`, …) is sync — build the query first, `await` only at the terminal.

`Entity.aselect()` and `Entity.aget()` are convenience shortcuts that locate
the `AsyncDatabase` automatically via `_find_db_for_entity`.

### Prefetch / N+1

`QuerySet.prefetch(*relations)` issues one extra `WHERE pk IN (...)` batch
query per relation after the main SELECT.  Access a relation attribute without
prefetch triggers a per-row lazy SELECT — classic N+1 risk.

### Migrations

`nextorm makemigrations` / `nextorm migrate` / `nextorm showmigrations` are
the CLI entry points (also available as `MigrationRunner` in Python).
Migration files live in a configurable directory; a `_migration_history` table
tracks applied versions.

---

## Coding Conventions

- Type annotations on all public functions and methods (enforced by pyright/mypy).
- Use **`from __future__ import annotations`** at the top of every module.
- Deferred imports inside functions to break circular dependencies use
  `# noqa: PLC0415`.
- `TYPE_CHECKING` guards for imports only needed by the type checker.
- Use `cast()` rather than `# type: ignore` where possible; use
  `# type: ignore[<code>]` with an explicit code when cast is not enough.
- For pyright only errors, prefer `# pyright: ignore[<code>]`.
- 100% **branch** coverage is required — every `if`/`else` path must be exercised.
- One test file per feature area (`test_entity.py`, `test_composite_pk.py`, …).
- Async tests use `@pytest.mark.asyncio`; `asyncio_mode = "auto"` is set globally.

---

## Quality Gates (required before finishing)

```bash
pdm run fix      # ruff check + fix if possible — no errors
pdm format       # ruff format — fix style
pdm typecheck    # pyright + mypy — no errors
pdm coverage     # pytest --cov — 100% branch coverage
pdm docs-html    # sphinx-build — 0 warnings
```

Any failure in any gate means the work is not complete.

---

## Common Pitfalls

- **Circular imports** — `entity.py`, `database.py`, and `async_database.py` all
  import each other.  Always use deferred `from x import y` inside functions for
  cross-module references; keep top-level imports inside `TYPE_CHECKING` blocks.
- **Generic class syntax** — `class PK[T]:` (PEP 695 new-style generics) crashes
  Sphinx autodoc on Python 3.12+.  Document these markers manually in
  `docs/api/fields.rst` with `.. py:class::` directives.
- **`typing.ForwardRef` in annotations** — causes `sphinx.util.typing.stringify_annotation`
  to crash on Python 3.13.  Keep `ForwardRef` out of public type annotations.
- **`pdm coverage` vs `pdm test`** — always use `pdm coverage` to verify the
  100% requirement; `pdm test` does not report coverage.
- **`db.close()` vs `await db.close()`** — `Database.close()` is sync;
  `AsyncDatabase.close()` is also sync (it schedules teardown).  There is no
  `await db.aclose()`.
