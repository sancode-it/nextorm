Getting Started
===============

Installation
------------

NextORM requires **Python 3.12 or later**.
Install the base package plus the driver for your database:

.. tab-set::

   .. tab-item:: SQLite

      .. code-block:: bash

         pip install nextorm[sqlite]

   .. tab-item:: PostgreSQL

      .. code-block:: bash

         pip install nextorm[postgres]

   .. tab-item:: MariaDB / MySQL

      .. code-block:: bash

         pip install nextorm[mariadb]

   .. tab-item:: All drivers

      .. code-block:: bash

         pip install "nextorm[sqlite,postgres,mariadb]"

Quick start
-----------

The minimal setup is:

1. Define entity classes that inherit from :class:`~nextorm.entity.Entity`.
2. Create a :class:`~nextorm.database.Database` and call :meth:`~nextorm.database.Database.bind`.
3. Call :meth:`~nextorm.database.Database.generate_mapping` to wire entities to database tables.
4. Wrap all data access in a :func:`~nextorm.session.db_session` context.

.. code-block:: python

   from nextorm import Database, Entity, PK, Req, Opt, db_session

   # Step 1 — define entities
   class User(Entity):
       id:    PK[int]
       name:  Req[str]
       email: Req[str]
       age:   Opt[int]

   # Step 2 — create and bind a database
   db = Database(entities=[User])
   db.bind("sqlite", "app.db")

   # Step 3 — create tables (dev/test only; use migrations in production)
   db.generate_mapping(create_tables=True)

   # Step 4 — work inside a session
   with db_session:
       user = User(name="Alice", email="alice@example.com", age=30)
   # ← INSERT fires automatically on commit; user.id is now set

   # Querying — using the class-level shortcut (no explicit db reference needed)
   users = User.select().filter(User.age >= 18).fetch_all()
   alice = User.get(email="alice@example.com")   # None if not found
   user  = User[1]                               # KeyError if not found

First steps with async
----------------------

For async applications swap :class:`~nextorm.async_database.AsyncDatabase` and
use ``await`` everywhere:

.. code-block:: python

   import asyncio
   from nextorm import AsyncDatabase, Entity, PK, Req, db_session

   class Task(Entity):
       id:    PK[int]
       title: Req[str]
       done:  Req[bool]

   async def main() -> None:
       db = AsyncDatabase(entities=[Task])
       await db.bind("sqlite", ":memory:")
       await db.generate_mapping(create_tables=True)

       async with db_session:
           Task(title="Buy milk", done=False)

       results = await db.aselect(Task).filter(Task.done == False).fetch_all()
       # or using the class-level shortcut:
       results = await Task.aselect().filter(Task.done == False).fetch_all()
       task = await Task.aget(title="Buy milk")   # None if not found
       print(results)

   asyncio.run(main())

Entity registration
-------------------

Entities register themselves globally as soon as their class is defined, so you
can pass ``entities=[...]`` to the :class:`~nextorm.database.Database` constructor
or call :meth:`~nextorm.database.Database.register` later — whichever is more
convenient.

If you have only one database instance you can omit ``entities`` entirely and
let :meth:`~nextorm.database.Database.generate_mapping` discover all entities
that have been imported:

.. code-block:: python

   import myapp.models  # imports ensure entity classes are defined
   db = Database()
   db.bind("sqlite", ":memory:")
   db.generate_mapping(create_tables=True)

Next steps
----------

- :doc:`entities` — field types, defaults, constraints, inheritance
- :doc:`queries` — filtering, ordering, pagination, aggregates
- :doc:`sessions` — transactions, commit, rollback, flush
- :doc:`relations` — one-to-many, many-to-many, eager loading
- :doc:`async` — async API and mixing sync/async databases
- :doc:`migrations` — schema migrations and CLI
