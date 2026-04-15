# Changelog

## 0.1.3 - 2026-04-15

- Make app version handling dynamic to avoid hardcoding in pyproject.toml and docs/conf.py
- Add NextORM icon and logos in dark, light, and neutral variants

## 0.1.2 - 2026-04-13

### Initial release of NextORM

Modern Python ORM with async support, full type annotations and a generator-expression query DSL

#### Features

- **Type-annotated fields** &mdash; `PK[int]`, `Req[str]`, `Opt[str]`, `Set[T]`, `Single[T]`
- **Auto-save sessions** &mdash; create entities inside `db_session` and they are committed automatically
- **PonyORM-compatible DSL** &mdash; generator-expression queries, `Entity[pk]`, `Entity.get()`, lifecycle hooks
- **Full async support** &mdash; `AsyncDatabase`, `await db.aselect(...)`, `Entity.aselect()`, `Entity.aget()`
- **Built-in migrations CLI** &mdash; `nextorm makemigrations` / `nextorm migrate` / `nextorm showmigrations`
- **Three providers** &mdash; SQLite, PostgreSQL (psycopg3), MariaDB
- **100% branch coverage** enforced in CI
