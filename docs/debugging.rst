Debugging & Performance
=======================

NextORM ships with built-in SQL logging and per-query statistics to help you
profile and debug your application.

SQL debug logging
-----------------

:func:`~nextorm.debug.set_sql_debug` enables or disables SQL logging globally.
When active, every statement and its parameters are printed to ``stderr`` (or a
custom file):

.. code-block:: python

   from nextorm import set_sql_debug

   set_sql_debug(True)
   users = db.select(User).filter(User.active == True).fetch_all()
   # stderr → >>> SELECT id, name FROM "user" WHERE active = ?
   #                params: [1]

   set_sql_debug(False)   # disable

Context-manager form
~~~~~~~~~~~~~~~~~~~~

Use :func:`~nextorm.debug.sql_debugging` as a context manager to scope SQL
logging to a specific block:

.. code-block:: python

   from nextorm import sql_debugging

   with sql_debugging():
       result = db.select(User).filter(User.id == 1).fetch_one()

SQL capture (for tests)
-----------------------

:class:`~nextorm.debug.capture_sql` records every SQL statement executed
within its scope into a :class:`~nextorm.debug.CapturedQuery` list — no
mocking needed:

.. code-block:: python

   from nextorm import capture_sql

   with capture_sql() as queries:
       db.select(User).fetch_all()
       db.select(Post).filter(Post.draft == False).fetch_all()

   assert len(queries) == 2
   assert "WHERE" in queries[1].sql
   assert queries[1].params == [False]

Each :class:`~nextorm.debug.CapturedQuery` has ``.sql`` and ``.params``
attributes; ``str(q)`` formats both for quick printing.

Nesting is supported; only the innermost context captures queries.

.. note::

   ``capture_sql`` is **sync-only** — it uses a thread-local stack and
   does not intercept queries issued by
   :class:`~nextorm.async_database.AsyncDatabase`. For async contexts use
   :class:`~nextorm.debug.async_capture_sql` instead.

Async SQL capture
~~~~~~~~~~~~~~~~~

:class:`~nextorm.debug.async_capture_sql` is the async counterpart of
:class:`~nextorm.debug.capture_sql`. It uses a :class:`contextvars.ContextVar`
so it works correctly across ``await`` boundaries:

.. code-block:: python

   from nextorm import async_capture_sql

   async with async_capture_sql() as queries:
       await db.aselect(User).fetch_all()
       await db.aselect(Post).filter(Post.draft == False).fetch_all()

   assert len(queries) == 2
   assert "WHERE" in queries[1].sql

.. note::

   ``async_capture_sql`` only captures queries issued by
   :class:`~nextorm.async_database.AsyncDatabase`. It does **not** capture
   sync queries from :class:`~nextorm.database.Database`. Use
   :class:`~nextorm.debug.capture_sql` for those.

Show SQL inline
---------------

To inspect the SQL that a specific :class:`~nextorm.query.QuerySet` would
produce without executing it:

.. code-block:: python

   qs = db.select(User).filter(User.age >= 18).order_by(User.name)
   print(qs.get_sql())
   # → SELECT id, name, age FROM "user" WHERE age >= ? ORDER BY name ASC

   qs.show()   # pretty-print the result set to stdout

Query statistics
----------------

NextORM tracks per-statement query counts and total execution time.

Global statistics
~~~~~~~~~~~~~~~~~

:func:`~nextorm.debug.global_stats` returns a dict mapping SQL text to
:class:`~nextorm.debug.QueryStat`:

.. code-block:: python

   from nextorm import global_stats, clear_global_stats

   clear_global_stats()
   # … run some operations …

   for sql, stat in global_stats().items():
       print(f"{sql[:60]:60}  count={stat.count}  total={stat.total_time:.3f}s")

Instance statistics
~~~~~~~~~~~~~~~~~~~

Each :class:`~nextorm.database.Database` instance records its own stats
independently:

.. code-block:: python

   # Access per-instance stats
   local = db.local_stats()
   db.clear_local_stats()

   # Merge instance stats into the global totals
   db.merge_local_stats()

The :class:`~nextorm.debug.QueryStat` dataclass:

.. code-block:: python

   @dataclass
   class QueryStat:
       sql: str           # the query text
       count: int         # number of executions
       total_time: float  # cumulative wall-clock seconds

Last executed SQL
~~~~~~~~~~~~~~~~~

Inspect the most recent SQL statement executed by a database instance:

.. code-block:: python

   users = db.select(User).fetch_all()
   print(db.last_sql)
   # → SELECT id, name FROM "user"

Async equivalent
----------------

Most debug utilities work identically with
:class:`~nextorm.async_database.AsyncDatabase`. The exception is
:class:`~nextorm.debug.capture_sql`, which is sync-only; use
:class:`~nextorm.debug.async_capture_sql` for async contexts.

.. code-block:: python

   await db.aselect(Post).filter(Post.draft == False).fetch_all()
   print(db.last_sql)
   local = db.local_stats()
