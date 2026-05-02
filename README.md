<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/_static/logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/_static/logo-light.svg">
    <img alt="NextORM" src="docs/_static/logo.svg">
  </picture>
</h1>


[![CI](https://github.com/sancode-it/nextorm/actions/workflows/ci.yml/badge.svg)](https://github.com/sancode-it/nextorm/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/nextorm)](https://pypi.org/project/nextorm/)
[![Python](https://img.shields.io/pypi/pyversions/nextorm)](https://pypi.org/project/nextorm/)
[![License](https://img.shields.io/pypi/l/nextorm)](LICENSE)
[![Docs](https://readthedocs.org/projects/nextorm/badge/?version=latest)](https://nextorm.readthedocs.io/en/latest/)

Modern Python ORM with async support, full type annotations and a generator-expression query DSL.

## Features

- **Type-annotated fields** &mdash; `PK[int]`, `Req[str]`, `Opt[str]`, `Set[T]`, `Single[T]`
- **Auto-save sessions** &mdash; create entities inside `db_session` and they are committed automatically
- **PonyORM-compatible DSL** &mdash; generator-expression queries, `Entity[pk]`, `Entity.get()`, lifecycle hooks
- **Full async support** &mdash; `AsyncDatabase`, `await db.aselect(...)`, `Entity.aselect()`, `Entity.aget()`
- **Built-in migrations CLI** &mdash; `nextorm makemigrations` / `nextorm migrate`
- **Three providers** &mdash; SQLite, PostgreSQL (psycopg3), MariaDB
- **100% branch coverage** enforced in CI

## Installation

```bash
pip install nextorm[sqlite]        # SQLite (aiosqlite)
pip install nextorm[postgres]      # PostgreSQL (psycopg3)
pip install nextorm[mariadb]       # MariaDB (asyncmy + PyMySQL)
pip install "nextorm[sqlite,postgres,mariadb]"   # all drivers
```

## Quick start

```python
from nextorm import Database, Entity, PK, Req, Opt, Set, Single, db_session

# Define entities — no database coupling required
class Tag(Entity):
    name: Req[str]
    products: Set["Product"]   # many-to-many back-reference

class Product(Entity):
    name:    Req[str](64)      # positional shorthand: max_len=64
    price:   Req[float]
    sku:     Req[str] = Req(column="product_sku", unique=True)  # marker-call options
    tags:    Set[Tag]  # many-to-many
    summary: Opt[str]  # Opt[str]/[LongStr] use empty string for None by default

# Create and connect the database
db = Database(entities=[Tag, Product])  # entities can also be auto-discovered
db.bind("sqlite", ":memory:")
db.generate_mapping(create_tables=True)

# Write — entities are tracked and committed automatically
with db_session:
    t = Tag(name="sale")
    p = Product(name="Widget", price=9.99, sku="WGT-1")
    p.tags.add(t)
# ← INSERT fires here; p.id and t.id are now set

# Read — class-level shortcuts (no explicit db reference needed)
widgets = Product.select().filter(Product.price < 20).fetch_all()
widget  = Product.get(name="Widget")   # None if not found
widget  = Product[1]                   # KeyError if not found
```

## Async quick start

```python
import asyncio
from nextorm import AsyncDatabase, Entity, PK, Req, db_session

class Task(Entity):
    title: Req[str]
    done:  Req[bool]

async def main() -> None:
    db = AsyncDatabase(entities=[Task])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)

    async with db_session:
        Task(title="Buy milk", done=False)

    pending = await Task.aselect().filter(Task.done == False).fetch_all()
    task    = await Task.aget(title="Buy milk")   # None if not found
    print(pending)

asyncio.run(main())
```

## Migrations

```bash
nextorm makemigrations   # generate a migration from model changes
nextorm migrate          # apply pending migrations
nextorm showmigrations   # list migration history
```

## Migrating from PonyORM

NextORM's API is intentionally close to PonyORM's. The main differences:

| PonyORM | NextORM |
| --- | --- |
| `class Product(db.Entity)` | `class Product(Entity)` |
| `Required(str)` | `Req[str]` |
| `Optional(str)` | `Opt[str]` |
| `PrimaryKey(int, auto=True)` | `PK[int]` |
| `Required(Order)` (FK side) | `Single[Order]` |
| `Set("Line")` (back-reference) | `Set["Line"]` |
| `select(p for p in Product if ...)` | identical |
| `Product[42]` | identical |
| `Product.get(name="x")` | identical |

See the [migration guide](https://nextorm.readthedocs.io/en/latest/ponyorm.html) for details.

## Documentation

Full docs at **[nextorm.readthedocs.io](https://nextorm.readthedocs.io/en/latest/)**.

## Development

```bash
pdm install
pdm test          # run tests
pdm coverage      # tests + branch coverage (must be 100%)
pdm typecheck     # pyright + mypy
pdm lint          # ruff
pdm format        # ruff format
pdm docs-html     # build Sphinx docs
```

## Acknowledgements

NextORM's API design and query DSL are heavily inspired by
[PonyORM](https://ponyorm.org), created by Alexander Kozlovsky, Alexey Malashkevich,
and Alexander Tischenko, released under the Apache License 2.0.
NextORM is a new, independent implementation and shares no source code with PonyORM.
