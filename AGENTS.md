# Agent Instructions for NextORM

NextORM is a modern Python ORM with async support, full type annotations, and a
PonyORM-inspired query DSL. Minimum Python version: **3.12** (tested on 3.12–3.14).

**v0.2 API**: Field declarations use marker-call syntax (`Req(...)`, `Opt(...)`, `Single(...)`,
`Set(...)`, `Local(...)`) instead of `FieldSpec`/`RelationSpec` class-body definitions.

---

## Quick-Start Agent Loop

1. **Before editing** — run `pdm test` and `pdm lint` to see the baseline state.
2. **After editing** — run `pdm run fix`, `pdm format`, `pdm typecheck`, `pdm coverage`.
3. **For API changes** — update or add docstrings; update the relevant narrative
   docs under `docs/` (e.g. `docs/collection.rst`, `docs/entities.rst`); rebuild
   docs with `pdm docs-html`.
4. **For any feature/API change** — add an entry to `CHANGELOG.rst` under the
   current unreleased version heading.
5. **Quality gate** — all tests must pass with **100% branch coverage** before work
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
  fields.py           Field markers: PK, Req, Opt, Set, Single, Local
                      (FieldSpec/RelationSpec for advanced/internal use)
  session.py          db_session context manager / decorator (sync & async)
                      flush/commit/rollback
  collection.py       RelatedCollection (lazy + prefetch collections)
  generators.py       Generator-expression query front-end (select/avg/sum/…)
                      Bytecode decompiler (Python 3.12–3.14 compatible)
  expr.py             ColumnExpr descriptor for column-level query nodes
  sql/                SQL AST nodes + builder (SQLiteBuilder, PostgresBuilder, …)
  schema/             DDL renderer + schema introspection
  migrations/         File-based migration runner + CLI
  providers/          Provider abstraction (SyncProvider, AsyncProvider, …)
  pool.py             ConnectionPool / AsyncConnectionPool
  debug.py            set_sql_debug, sql_debugging, capture_sql, async_capture_sql,
                      CapturedQuery, QueryStat, global_stats, clear_global_stats
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

### Unified Query API

All entry-point methods share the same calling convention:
`method(predicate=None, *conditions: SqlNode, **kwargs)` — all arguments optional.

| Method | Returns |
|---|---|
| `Entity.select(predicate?, *conditions, **kwargs)` | `QuerySet[Self]` |
| `Entity.aselect(predicate?, *conditions, **kwargs)` | `AsyncQuerySet[Self]` |
| `Entity.get(predicate?, *conditions, **kwargs)` | `Self \| None` |
| `Entity.aget(predicate?, *conditions, **kwargs)` | `Self \| None` |
| `Entity.exists(predicate?, *conditions, **kwargs)` | `bool` |
| `RelatedCollection.select(predicate?, *conditions, **kwargs)` | `QuerySet[T]` |
| `RelatedCollection.where(predicate)` | `QuerySet[T]` |
| `RelatedCollection.filter(*conditions, **kwargs)` | `QuerySet[T]` |
| `QuerySet.where(predicate)` | `QuerySet[ET]` |
| `QuerySet.filter(*conditions, **kwargs)` | `QuerySet[ET]` |

### Prefetch / N+1

`QuerySet.prefetch(*relations)` issues one extra `WHERE pk IN (...)` batch
query per relation after the main SELECT.  Access a relation attribute without
prefetch triggers a per-row lazy SELECT — classic N+1 risk.

### Migrations

`nextorm makemigrations` / `nextorm migrate` / `nextorm showmigrations` are
the CLI entry points (also available as `MigrationRunner` in Python).
Migration files live in a configurable directory; a `_migration_history` table
tracks applied versions.

### Field Markers (v0.2)

**Scalar markers:**
- `Req[T]` — required field; supports positional arg: `Req[str](max_len)`, `Req[int](size)`
- `Opt[T]` — optional field (nullable); same positional args as `Req[T]`
  - **Special behavior:** `Opt[str]` and `Opt[LongStr]` store as empty strings by default; only `nullable=True` allows NULL
  - **All other `Opt[T]`** are nullable (allow NULL) by default
- `Local[T]` — transient field (not persisted); supports `default` and `py_check` callable
- `PK[T]` — primary key; if `T` is an `Entity`, creates a `Single` relation

**Relation markers:**
- `Single[Entity]` — one-to-one or many-to-one
- `Set[Entity]` — one-to-many or many-to-many

**Relation options** (`Single` and `Set`):
- `column` — override FK column name (Single only)
- `reverse_column` — override reverse FK column name in join table (Set only)
- `fk_name` — override foreign key constraint name
- `table` — override join table name (many-to-many only)
- `nullable` — allow NULL in FK (Single only)
- `cascade_delete` — delete related rows on parent delete
- `owner` — ownership direction for one-to-one (Single only)

**Example:**
```python
class Order(Entity):
    id = PK[int]
    customer = Single[Customer](fk_name="fk_order__customer", cascade_delete=True)
    items = Set[OrderItem](cascade_delete=True)
    created = Req[datetime](precision=6)
    notes = Opt[str](256)
    is_draft = Local[bool](default=True)
```

### Debugging & SQL Capture

#### Sync SQL capture (`capture_sql`)

Use `capture_sql` to assert on executed SQL in tests — no mocking required:

```python
from nextorm import capture_sql

with capture_sql() as queries:
    db.select(User).filter(User.active == True).fetch_all()

assert len(queries) == 1
assert "WHERE" in queries[0].sql
assert queries[0].params == [True]
```

Key properties of `capture_sql`:
- Returns a `list[CapturedQuery]`; each entry has `.sql` (str) and `.params` (list).
- **Sync only** — captures only queries issued by `Database` and `QuerySet`.
- Nesting is supported; only the **innermost** block captures queries.
- The list remains accessible after the `with` block exits.
- `str(captured_query)` formats as `"SQL  -- params: [...]"` for quick printing.

#### Async SQL capture (`async_capture_sql`)

For async code using `AsyncDatabase` and `AsyncQuerySet`, use `async_capture_sql`:

```python
from nextorm import async_capture_sql

async with async_capture_sql() as queries:
    await db.aselect(User).fetch_all()
    await db.aselect(Post).filter(Post.draft == False).fetch_all()

assert len(queries) == 2
assert "WHERE" in queries[1].sql
assert queries[1].params == [False]
```

Key properties of `async_capture_sql`:
- Uses `contextvars.ContextVar` so it works correctly across `await` boundaries.
- **Async only** — captures only queries from `AsyncDatabase` and `AsyncQuerySet`.
- Nesting is supported; only the **innermost** block captures queries.
- Each context captures independently into its own list.
- Works seamlessly with pytest's `@pytest.mark.asyncio` decorator.

#### Debug output

For human-readable debug output during development:

```python
from nextorm import sql_debugging

with sql_debugging():
    # All SQL + params printed to stdout (sync)
    db.select(Order).fetch_all()

# For async, use set_sql_debug(True) or sql_debugging() then check stdout:
from nextorm import set_sql_debug

set_sql_debug(True)
# async code will now also print SQL debug output
```

### Session and db_session

`db_session` is used for **both sync and async** contexts:
- Sync: `with db_session: ...`
- Async: `async with db_session: ...` (inside `async def`)

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
- **Python 3.14 bytecode** — `generators.py` handles `LOAD_SMALL_INT` (optimized
  integer constants) and `POP_ITER` (loop cleanup).  On older Python versions,
  `LOAD_CONST` is used instead.  No action needed; bytecode decompiler is
  version-agnostic.
- **API v0.2 syntax** — Never use old `FieldSpec()`/`RelationSpec()` class-body
  syntax in new code.  Use marker-call syntax: `Req(...)`, `Opt(...)`, `Single(...)`,
  `Set(...)`, `Local(...)`.
- **v0.2 `db_session` for both** — `db_session` works for sync and async;
  there is no `async_db_session`.  Use the same context manager for both.
- **Table management is destructive** — `drop_table`, `drop_all_tables`, and
  `Entity.drop_table()` are irreversible.  The default `with_all_data=False`
  guard raises `RuntimeError` when rows exist; always pass `with_all_data=True`
  intentionally.  `RelatedCollection.drop_table()` is only valid on M2M
  relations — calling it on O2M raises `RuntimeError`.
- **`disconnect()` vs `close()`** — `Database.disconnect()` drains the
  connection pool (useful before deleting a SQLite file); `Database.close()` is
  the full teardown (pool + metadata).  Call `disconnect()` when you only want
  to release open handles and keep the `Database` object reusable.
