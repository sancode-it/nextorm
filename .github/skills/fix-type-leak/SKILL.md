---
name: fix-type-leak
description: 'Fix type errors leaking from NextORM into consumer apps. Use when a consumer app raises a type error that originates in the NextORM library — not in the consumer code. Use for: type: ignore in consumer app code caused by NextORM, pyright or mypy errors (type errors) pointing at nextorm imports, incorrect return types on QuerySet/Entity methods, missing overloads, incorrect generic bounds, suppressed type errors in NextORM source, untyped public API.'
argument-hint: 'Paste the type error or describe the affected NextORM API'
---

# Fix NextORM Type Leak

Consumer apps must never need `# type: ignore` comments because of NextORM.
Any type error that originates in the NextORM library is a bug in NextORM, not
in the consumer.

## When to Use

- A consumer app gets a pyright or mypy error on a line that imports or calls
  a NextORM API (e.g. `db.select(...)`, `Entity.aget()`, a field access).
- A consumer reports they had to add `# type: ignore` to work around NextORM.
- A public method, property, or generic class has a return type of `Any` or
  is missing type annotations entirely.
- A public method, property, or class is missing `py.typed` marker visibility.

## Procedure

### 1. Reproduce the Error

```bash
# From the consumer app, run both type checkers — record the full error:
pyright .
mypy .

# From the NextORM repo, attempt to confirm it's a NextORM problem:
cd /path/to/nextorm
pdm typecheck
```

`pdm typecheck` passing does **not** guarantee the type is correct for consumers.
NextORM source may suppress errors internally with `# type: ignore` or
`# pyright: ignore` comments that hide the root cause.

**Surface suppressed errors with mypy:**

```bash
# To see ALL errors even where ignore comments exist, strip them temporarily:
grep -rn 'type: ignore\|pyright: ignore' nextorm/
```

**Surface suppressed errors with pyright:**

```bash
# verifytypes checks public API type completeness (missing annotations, Any leaks)
pdm pyright --verifytypes nextorm --ignoreexternal

# To bypass specific pyright: ignore comments, temporarily remove them:
# (pyright has no flag to override suppression comments globally)
```

**Temporarily strip all suppression comments for diagnosis** (restore afterwards):

```bash
# Strip type: ignore comments from a specific file, run pyright, then restore:
cp nextorm/query.py nextorm/query.py.bak
sed -i 's/  # type: ignore\[[^]]*\]//g; s/  # pyright: ignore\[[^]]*\]//g' nextorm/query.py
pyright nextorm/query.py
mv nextorm/query.py.bak nextorm/query.py
```

If the consumer error points at a symbol that has nearby suppression comments
in NextORM, those comments may be masking the real type problem. Remove or
narrow them as part of the fix.

### 2. Locate the Failing API

Identify the exact public symbol that the consumer uses:
- What module is it imported from? (e.g. `from nextorm import QuerySet`)
- Is it a method, property, class, or overload?
- What type does pyright/mypy infer vs. what is expected?

Check in this order:
1. `nextorm/__init__.py` — Is the symbol re-exported correctly?
2. The source file that defines it — Is the annotation correct and complete?

Note: NextORM ships with `py.typed` and has no stub files of its own.
The `stubs/` directory only contains stubs for third-party dependencies
(e.g. `asyncmy`). Do not create or modify stubs for NextORM itself.

### 3. Common Fixes

#### Missing return type annotation

Add the return type. Never use `Any` in a public signature:

```python
# Bad
def fetch_all(self):
    ...

# Good
def fetch_all(self) -> list[T]:
    ...
```

#### Incorrect generic parameter

Check that `TypeVar` bounds are tight enough and that the class uses PEP 695
new-style generics (`class QuerySet[T]:`) consistently:

```python
# Bad — T is unconstrained, infers as Unknown in consumer
class QuerySet[T]:
    def get(self) -> T: ...

# Good — if T must be an Entity subclass, bound it
class QuerySet[T: Entity]:
    def get(self) -> T: ...
```

#### Missing overload for multi-signature methods

When a method can return different types based on arguments, use `@overload`:

```python
from typing import overload

@overload
def select(self, entity: type[T]) -> QuerySet[T]: ...
@overload
def select(self, *columns: ColumnExpr) -> QuerySet[tuple[Any, ...]]: ...
def select(self, *args):  # actual implementation
    ...
```

#### Symbol not exported from `__init__.py`

Consumer gets `Module has no attribute "X"`. Add it to `nextorm/__init__.py`
and to `__all__` if defined:

```python
from .query import QuerySet as QuerySet
```

### 4. Verify Fix

Run the full quality gate — all must pass before the fix is complete:

```bash
cd /path/to/nextorm
pdm run fix        # auto-fix ruff issues
pdm format         # format
pdm typecheck      # pyright + mypy — MUST report 0 errors
pdm coverage       # all tests pass, 100% branch coverage
```

Then verify in the consumer app:

```bash
cd /path/to/consumer
pyright .          # no errors on the previously-failing line
mypy .             # no errors on the previously-failing line
```

### 5. Add a Regression Test

Type correctness is not covered by pytest alone. Add a type-checking test
using `assert_type` (stdlib) or a comment-based approach so future changes
can't re-introduce the regression:

```python
# tests/test_types.py
from typing import assert_type
from nextorm import QuerySet, Entity

class MyEntity(Entity):
    id = PK[int]

db = Database(...)
qs = db.select(MyEntity)
assert_type(qs, QuerySet[MyEntity])  # must not be QuerySet[Any]
```

Pyright and mypy both honour `assert_type` and will fail if the inferred type
doesn't match.

## Quick Checklist

- [ ] Reproduced the error in the consumer app
- [ ] Confirmed the error originates in NextORM, not the consumer
- [ ] Identified the exact public symbol and its source file (no NextORM stubs — fix the source directly)
- [ ] Applied one of the common fixes above
- [ ] `pdm typecheck` passes with 0 errors
- [ ] `pdm coverage` passes with 100% branch coverage
- [ ] Consumer app pyright/mypy passes with no `# type: ignore` needed
- [ ] Regression test added with `assert_type`
