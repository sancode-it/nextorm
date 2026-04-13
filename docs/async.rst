Async Usage
===========

NextORM provides a full async API through :class:`~nextorm.async_database.AsyncDatabase`.
Every method that touches the database has an ``async`` counterpart — you never need to
mix sync and async styles within a single :class:`~nextorm.async_database.AsyncDatabase`.

AsyncDatabase
-------------

:class:`~nextorm.async_database.AsyncDatabase` is the async mirror of
:class:`~nextorm.database.Database`.  Create and use it entirely with ``await``:

.. code-block:: python

   import asyncio
   from nextorm import AsyncDatabase, Entity, PK, Req, db_session

   class Post(Entity):
       id:    PK[int]
       title: Req[str]
       body:  Req[str]

   async def main() -> None:
       db = AsyncDatabase(entities=[Post])
       await db.bind("sqlite", ":memory:")
       await db.generate_mapping(create_tables=True)

       # INSERT via session (auto-save, same as sync)
       async with db_session:
           p = Post(title="Hello", body="World")
       # ← INSERT fires automatically on __aexit__; p.id is now available
       print(p.id)

       # Or save explicitly outside a session:
       p2 = Post(title="World", body="Hello")
       await db.asave(p2)
       print(p2.id)        # available after asave()

       # SELECT
       posts = await db.aselect(Post).filter(Post.title == "Hello").fetch_all()

       # DELETE
       await db.adelete_instance(posts[0])

       await db.close()

   asyncio.run(main())

Context manager
~~~~~~~~~~~~~~~

:class:`~nextorm.async_database.AsyncDatabase` also works as an async context manager
that binds and closes the connection automatically:

.. code-block:: python

   async with db:
       results = await db.aselect(Post).fetch_all()

CRUD methods
------------

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Method
     - Description
   * - ``await db.asave(entity)``
     - INSERT (new entity) or UPDATE (dirty entity).
   * - ``await db.ainsert(entity)``
     - Force INSERT even if the entity already has a PK.
   * - ``await db.adelete_instance(entity)``
     - DELETE the row for the given entity instance.
   * - ``await db.acommit()``
     - Flush + COMMIT.
   * - ``await db.arollback()``
     - ROLLBACK + clear identity map.
   * - ``await db.aflush()``
     - Write pending changes without committing.

Async queries
-------------

:meth:`~nextorm.async_database.AsyncDatabase.aselect` returns an
:class:`~nextorm.async_database.AsyncQuerySet`.  Every terminal method is a coroutine:

.. code-block:: python

   results = await db.aselect(Post).filter(Post.title == "Hello").fetch_all()
   first   = await db.aselect(Post).filter(Post.id == 1).fetch_one()
   n       = await db.aselect(Post).count()
   exists  = await db.aselect(Post).filter(Post.id == 99).exists()
   deleted = await db.aselect(Post).filter(Post.draft == True).delete()
   updated = await db.aselect(Post).filter(Post.draft == True).update(draft=False)

Class-level shortcuts
~~~~~~~~~~~~~~~~~~~~~

Just like the sync API, you can skip the explicit ``db`` reference:

.. code-block:: python

   # Entity.aselect() — equivalent to db.aselect(Entity)
   results = await Post.aselect().filter(Post.draft == False).fetch_all()

   # Entity.aget() — returns None if no match; raises if multiple rows match
   post = await Post.aget(title="Hello")

   # Primary key subscript works for sync sessions; use aget for async:
   post = await Post.aget(id=1)

Async sessions
--------------

:func:`~nextorm.session.db_session` (and its alias ``async_db_session``) works
as an async context manager and decorator:

.. code-block:: python

   from nextorm import db_session

   async with db_session:
       Post(title="Draft", body="…")   # auto-scheduled for INSERT
   # ← aflush() + acommit() fired automatically on __aexit__

   @db_session
   async def create_post(title: str, body: str) -> None:
       Post(title=title, body=body)

Mixing sync and async databases
--------------------------------

A single ``db_session`` context can span both
:class:`~nextorm.database.Database` and
:class:`~nextorm.async_database.AsyncDatabase` instances simultaneously.
The ``__aexit__`` path of :func:`~nextorm.session.db_session` detects
database type at runtime and dispatches accordingly:

.. code-block:: python

   sync_db  = Database(entities=[User])
   async_db = AsyncDatabase(entities=[Post])

   sync_db.bind("sqlite", "users.db")
   sync_db.generate_mapping()

   await async_db.bind("sqlite", "posts.db")
   await async_db.generate_mapping()

   async with db_session:
       User(name="alice")           # tracked by sync_db
       Post(title="Hello", body="…") # tracked by async_db
   # ← flushes sync and async DBs, then commits both primary-first

Connection providers
--------------------

:class:`~nextorm.async_database.AsyncDatabase` supports all three providers:

.. tab-set::

   .. tab-item:: SQLite (aiosqlite)

      .. code-block:: python

         await db.bind("sqlite", ":memory:")
         # or a file:
         await db.bind("sqlite", "/tmp/app.db")

   .. tab-item:: PostgreSQL (psycopg3)

      .. code-block:: python

         await db.bind(
             "postgres",
             host="localhost",
             port=5432,
             dbname="myapp",
             user="app",
             password="secret",
         )

   .. tab-item:: MariaDB (asyncmy)

      .. code-block:: python

         await db.bind(
             "mariadb",
             host="localhost",
             user="app",
             password="secret",
             db="myapp",
         )

Async connection pool
---------------------

Enable connection pooling by passing pool parameters to ``bind``:

.. code-block:: python

   await db.bind(
       "postgres",
       host="localhost", dbname="myapp", user="app", password="s3cr3t",
       pool_min=2, pool_max=10, pool_timeout=30.0,
   )

See :doc:`pooling` for details.

Raw SQL
-------

.. code-block:: python

   rows_affected = await db.execute("UPDATE post SET draft = FALSE WHERE published < ?", [cutoff])
   rows = await db.select_raw("SELECT id, title FROM post WHERE draft = ?", [False])
