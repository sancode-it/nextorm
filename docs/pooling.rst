Connection Pooling
==================

Both :class:`~nextorm.database.Database` and :class:`~nextorm.async_database.AsyncDatabase`
support optional connection pooling.  Pooling is engaged by passing pool
parameters to :meth:`~nextorm.database.Database.bind`.

Enabling a pool
---------------

.. tab-set::

   .. tab-item:: Sync (Database)

      .. code-block:: python

         from nextorm import Database, Entity, Req

         class User(Entity):
             name: Req[str]

         db = Database(entities=[User])
         db.bind(
             "sqlite",
             "app.db",
             pool_min=2,       # pre-create 2 connections
             pool_max=10,      # block once 10 connections are checked out
             pool_timeout=30.0,# raise PoolTimeoutError after 30 s
         )
         db.generate_mapping()

   .. tab-item:: Async (AsyncDatabase)

      .. code-block:: python

         from nextorm import AsyncDatabase, Entity, Req

         class Post(Entity):
             title: Req[str]

         db = AsyncDatabase(entities=[Post])
         await db.bind(
             "postgres",
             host="localhost", dbname="myapp", user="app", password="s3cr3t",
             pool_min=2,
             pool_max=20,
             pool_timeout=10.0,
         )
         await db.generate_mapping()

Parameters
----------

All three parameters are optional.  Omitting them entirely keeps the
single-persistent-connection behaviour (the default).

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Parameter
     - Default
     - Description
   * - ``pool_min``
     - ``0``
     - Number of connections to pre-create at ``bind()`` time.
   * - ``pool_max``
     - ``1``
     - Maximum concurrent checked-out connections.  Callers block until
       a connection is returned or ``pool_timeout`` expires.
   * - ``pool_timeout``
     - ``30.0``
     - Seconds to wait for a free connection before raising
       :exc:`~nextorm.pool.PoolTimeoutError`.

How it works
------------

The :class:`~nextorm.pool.ConnectionPool` (sync) and
:class:`~nextorm.pool.AsyncConnectionPool` (async) maintain a bounded FIFO
queue of connections.  Each query or DML statement checks out a connection
at the start and returns it at the end.  The pool is safe to use from
multiple threads (sync) or concurrent coroutines (async).

- If ``pool_min > 0``, connections are opened eagerly at bind time.
- If a checkout request arrives and ``pool_max`` connections are already in
  use, the caller sleeps for up to ``pool_timeout`` seconds.
- Connections that raise an error during a query are discarded; the pool
  creates a fresh replacement.

Closing the pool
----------------

When you are done with the database instance, call
:meth:`~nextorm.database.Database.close` (or ``await db.close()`` for
:class:`~nextorm.async_database.AsyncDatabase`).  This closes all pooled
connections:

.. code-block:: python

   db.close()         # closes pool and underlying connections

:exc:`~nextorm.pool.PoolTimeoutError`
--------------------------------------

Raised when all pool connections are in use and the timeout expires:

.. code-block:: python

   from nextorm.pool import PoolTimeoutError

   try:
       results = db.select(User).fetch_all()
   except PoolTimeoutError:
       # handle: pool exhausted
       ...
