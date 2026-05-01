Sessions & Transactions
=======================

NextORM uses an *identity map session* — a lightweight cache that ensures at
most one Python object exists per database row within a given scope.  The session
also tracks dirty instances and defers writes until commit time.

The ``db_session`` context manager
-----------------------------------

All reads, writes, and relation accesses should take place inside a
:func:`~nextorm.session.db_session` context:

.. code-block:: python

   from nextorm import db_session

   with db_session:
       user = User(name="alice")       # scheduled for INSERT
       user.name = "bob"               # marked dirty → UPDATE on commit
   # ← session ends: flush + commit happen automatically

Using :func:`~nextorm.session.db_session` as a **decorator** wraps an entire
function in a session:

.. code-block:: python

   @db_session
   def create_user(name: str) -> None:
       User(name=name)

Session lifecycle
-----------------

1. **Enter** — a new :class:`~nextorm.session.SessionCache` (identity map) is pushed.
2. **Work** — entities created or loaded inside the session are tracked.
3. **Exit (clean)** — :meth:`~nextorm.database.Database.flush` is called first to
   write all pending inserts/updates; then every database is committed, primary-first.
4. **Exit (exception)** — every database is rolled back; the exception propagates.

Dirty tracking
~~~~~~~~~~~~~~

When you modify an attribute on a loaded entity, the session marks it dirty
automatically:

.. code-block:: python

   with db_session:
       user = db.select(User).filter(User.id == 1).get()
       user.name = "alice"   # ← session sees this change
   # ← UPDATE user SET name = 'alice' WHERE id = 1

Nested sessions
---------------

Nested ``with db_session:`` calls reuse the outermost session's identity map.
The commit only happens when the **outermost** session exits:

.. code-block:: python

   with db_session:            # outermost — creates identity map
       with db_session:        # nested — shares the same cache
           User(name="inner")
       # ← inner exit: no commit yet
   # ← outermost exit: flush + commit

Parametrised sessions
---------------------

Pass keyword arguments to configure the session behaviour:

.. code-block:: python

   with db_session(sql_debug=True):          # log every SQL statement
       ...

   with db_session(serializable=True):       # SERIALIZABLE isolation (if supported)
       ...

   with db_session(optimistic=False):        # disable per-field optimistic checks
       ...

   @db_session(retry=3):                     # retry on TransactionError up to 3 times
   def do_work() -> None:
       ...

   @db_session(allowed_exceptions=[KeyError]):   # suppress KeyError, commit normally
   def process() -> None:
       raise KeyError("ok")   # ← session commits; KeyError is re-raised after cleanup

Flushing
--------

:meth:`~nextorm.database.Database.flush` writes all pending objects to the database
**without committing the transaction**. Use it when you need the auto-generated
primary key of an object inside the same session:

.. code-block:: python

   from nextorm import flush, db_session

   with db_session:
       order = Order(total=99.0)
       flush()                 # INSERT fires → order.id is now available
       line = OrderLine(order_id=order.id, product_id=42, qty=1)

The module-level :func:`~nextorm.__init__.flush` flushes all databases that have
pending entities in the current session.

Manual commit and rollback
--------------------------

Within a session you can commit or roll back manually:

.. code-block:: python

   from nextorm import commit, rollback, db_session

   with db_session:
       User(name="alice")
       commit()               # flush + commit — session continues
       User(name="bob")
       rollback()             # discard "bob", clear identity map

Multi-database commit
---------------------

When a session spans multiple databases, NextORM uses a *staged commit*:

1. **Flush all** — write pending changes to every database.  If flush fails,
   roll back all databases.
2. **Commit primary** — commit the first database.  If this fails, roll back
   all secondaries and raise :exc:`~nextorm.exceptions.CommitException`.
3. **Commit secondaries** — commit each remaining database. If any fail, a
   :exc:`~nextorm.exceptions.PartialCommitException` is raised (the primary
   commit is already durable).

The *primary* database is the one whose entity appears first in the session's
identity map.

Session depth and introspection
--------------------------------

.. code-block:: python

   from nextorm.session import db_session

   print(db_session.depth)        # 0 outside, ≥1 inside
   cache = db_session.current()   # SessionCache | None

Async sessions
--------------

.. code-block:: python

   from nextorm import db_session

   async with db_session:
       await db.asave(User(name="alice"))

The ``__aexit__`` path is identical to ``__exit__`` except async databases have
their flush/commit/rollback awaited.

Exceptions
----------

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Exception
     - Raised when
   * - :exc:`~nextorm.exceptions.TransactionError`
     - A deadlock, serialisation failure, or invalid transaction state is
       detected. Retried automatically when ``retry=`` is set.
   * - :exc:`~nextorm.exceptions.OptimisticCheckError`
     - An optimistic concurrency check fails (another process changed a
       field you read since your last load).
   * - :exc:`~nextorm.exceptions.CommitException`
     - The primary-database commit fails in a multi-database session.
   * - :exc:`~nextorm.exceptions.PartialCommitException`
     - One or more secondary-database commits fail (primary already durable).
