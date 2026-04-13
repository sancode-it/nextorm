Changelog
=========

0.1.0 — unreleased
-------------------

Initial release of NextORM, a modern Python ORM with async support and full type
annotations.

Features:

- Entity definitions with ``PK``, ``Req``, ``Opt``, and relation fields
- Query DSL with generator expressions and lambda filters
- Eager loading and pagination utilities
- File-based migrations with CLI commands
- Identity map and session integration for caching and dirty tracking
- Thread safety with ``threading.local`` and ``ContextVar``
