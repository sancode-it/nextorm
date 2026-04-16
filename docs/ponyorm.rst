Migrating from PonyORM
======================

NextORM is heavily inspired by PonyORM — the query DSL, session model, and
relation system will feel familiar.  This page covers what changed and what
to watch out for.

Overview
--------

The biggest conceptual shift is that entities are **decoupled from the database
instance**.  In PonyORM every entity inherits from ``db.Entity``; in NextORM you
subclass ``Entity`` directly and the database discovers entities automatically (or
you scope them explicitly).

Everything else — ``db_session``, ``commit()``, ``rollback()``, ``flush()``,
the generator-expression query syntax, lifecycle hooks — works the same way.

Entity definition
-----------------

The most visible change is field declaration syntax.  PonyORM uses descriptor
calls; NextORM uses type annotations:

.. code-block:: python

   # PonyORM
   class Product(db.Entity):
       name  = Required(str)
       price = Optional(float)
       id    = PrimaryKey(int, auto=True)

   # NextORM
   class Product(Entity):
       name:  Req[str]
       price: Opt[float]
       id:    PK[int]          # auto-increment is the default for PK[int]

The table name defaults to the class name lower-cased (same as PonyORM).
Override it with ``_table_name_ = "my_table"``.

Relations
---------

PonyORM infers relationship types from the combination of attributes on both
sides.  NextORM requires an explicit ``Single[T]`` (FK side) and ``Set[T]``
(collection side):

.. code-block:: python

   # PonyORM
   class Order(db.Entity):
       lines = Set('OrderLine')

   class OrderLine(db.Entity):
       order = Required(Order)    # PonyORM infers the FK from Required(...)

   # NextORM
   class Order(Entity):
       lines: Set["OrderLine"]    # back-reference

   class OrderLine(Entity):
       order: Single[Order]       # explicit FK

``Single[T]`` creates a ``NOT NULL`` FK with ``ON DELETE CASCADE``.
``Single[T | None]`` creates a nullable FK with ``ON DELETE SET NULL``.

Querying
--------

The generator syntax is identical:

.. code-block:: python

   # Both PonyORM and NextORM
   from nextorm.generators import select
   results = select(p for p in Product if p.price > 100).fetch_all()

The main difference is the **terminal call**.  PonyORM lets you iterate a query
or slice it (``q[:]``).  NextORM always requires an explicit terminal:

.. code-block:: python

   # PonyORM
   results = list(select(p for p in Product))
   first   = select(p for p in Product).first()

   # NextORM
   results = select(p for p in Product).fetch_all()
   first   = select(p for p in Product).first()

The fluent ``QuerySet`` API is also available, including a class-level shortcut
that locates the database automatically:

.. code-block:: python

   # via db reference
   db.select(Product).filter(Product.price > 100).order_by(Product.price).fetch_all()

   # class-level shortcut (no explicit db needed)
   Product.select().filter(Product.price > 100).fetch_all()

   # async equivalents
   await db.aselect(Product).filter(Product.price > 100).fetch_all()
   await Product.aselect().filter(Product.price > 100).fetch_all()

   # classic Entity.get() style
   user = User.get(email="alice@example.com")             # sync
   user = await User.aget(email="alice@example.com")      # async

Primary key lookup
-------------------

.. code-block:: python

   # PonyORM
   p = Product[42]

   # NextORM — same syntax; raises KeyError when not found
   p = Product[42]

   # Composite PK — pass individual values (Python tuple syntax)
   line = OrderLine[order_id, product_id]

Saving and deleting
--------------------

NextORM tracks new instances automatically — the same as PonyORM.
Creating an entity inside a ``db_session`` schedules it for INSERT; the
session commits the change on exit:

.. code-block:: python

   # Both PonyORM and NextORM work the same way
   with db_session:
       p = Product(name="Widget", price=9.99)
   # ← INSERT fires automatically on commit

When you need the auto-generated PK before the session ends, call
:func:`~nextorm.session.flush` explicitly:

.. code-block:: python

   with db_session:
       p = Product(name="Widget", price=9.99)
       flush()            # INSERT → p.id is now available
       print(p.id)

:meth:`~nextorm.database.Database.save` and
:meth:`~nextorm.database.Database.insert` are also available when you want
fine-grained control or need to insert outside a session context.

Deletion is the same in both ORMs:

.. code-block:: python

   p.delete()              # shortcut (entity must have been loaded via a session)
   db.delete_instance(p)   # explicit alternative

N+1 and eager loading
----------------------

PonyORM automatically batches FK look-ups into a single ``WHERE pk IN (...)``
query when it detects you are iterating a result set.  NextORM does not do
this automatically — you must declare relations to prefetch:

.. code-block:: python

   # Without prefetch — one extra query per post (N+1)
   posts = db.select(Post).fetch_all()
   for post in posts:
       print(post.author.name)   # lazy SELECT each time

   # With prefetch — one batch query for all authors
   posts = db.select(Post).prefetch(Post.author).fetch_all()
   for post in posts:
       print(post.author.name)   # pre-populated, no extra SQL

What is not yet implemented
----------------------------

A few PonyORM features are not yet available in NextORM:

- **Transparent lazy-load batching** (PonyORM's N+1 coalescing) — use
  ``prefetch()`` explicitly.
- ``@db_session(retry=n)`` — session-level automatic retry on deadlock.
- ``Entity.select(lambda)`` — passing a lambda *directly* to
  ``Entity.select()`` is not supported; use ``Entity.select().where(lambda)``
  or the generator-expression syntax ``select(x for x in Entity if ...)``
  instead.
- Hybrid properties (usable inside both Python and generator queries).
- Oracle and CockroachDB providers — SQLite, PostgreSQL, and MariaDB are
  fully supported.

