The Query API
=============

All queries are built through the :class:`~nextorm.query.QuerySet` API.
Use :meth:`~nextorm.database.Database.select` to start a query and chain methods
to refine it; nothing is sent to the database until you call a terminal method.

Starting a query
----------------

.. code-block:: python

   qs = db.select(User)           # SELECT * FROM user

   # Class-level shortcut — locates the database automatically
   qs = User.select()             # equivalent; no explicit ``db`` reference needed

All chain methods return a new :class:`~nextorm.query.QuerySet` — the original
is never modified.

Fetch one by primary key or field values
-----------------------------------------

The most common lookups have class-level shortcuts that avoid writing a full
query:

.. code-block:: python

   # Primary key subscript — raises KeyError when not found
   user = User[1]

   # Composite PK — list values in declaration order
   line = OrderLine[order_id, product_id]

   # get() — returns None when no match; raises if multiple rows match
   user = User.get(email="alice@example.com")
   user = User.get(name="alice", age=30)   # multiple kwargs are ANDed

   # exists() — True/False
   if User.exists(email="alice@example.com"):
       ...

Filtering
---------

Column expressions
~~~~~~~~~~~~~~~~~~

The most direct way to filter is to compare entity class attributes:

.. code-block:: python

   db.select(User).filter(User.name == "alice").fetch_all()
   db.select(User).filter(User.age >= 18, User.active == True).fetch_all()

Multiple ``filter()`` arguments are combined with ``AND``.  Chain several
:meth:`~nextorm.query.QuerySet.filter` calls for the same effect:

.. code-block:: python

   db.select(Product).filter(Product.price > 10).filter(Product.stock > 0)

Keyword equality shortcuts
~~~~~~~~~~~~~~~~~~~~~~~~~~

``filter()`` also accepts keyword arguments as field-equality shortcuts:

.. code-block:: python

   db.select(User).filter(active=True).fetch_all()
   db.select(User).filter(User.age >= 18, active=True).fetch_all()

Keyword arguments are ``AND``-ed with any positional conditions.

Lambda predicates
~~~~~~~~~~~~~~~~~

The :meth:`~nextorm.query.QuerySet.where` method accepts a lambda to express
conditions without repeating the class name:

.. code-block:: python

   db.select(Product).where(lambda p: p.price > 10).fetch_all()
   db.select(User).where(lambda u: u.name == "alice").fetch_all()

Generator syntax
~~~~~~~~~~~~~~~~

Import :func:`~nextorm.generators.select` for a PonyORM-style generator expression:

.. code-block:: python

   from nextorm import select

   results = select(u for u in User if u.age >= 18 and u.active == True)

.. note::

   Generator queries work by decompiling the lambda's bytecode.  Complex
   Python expressions (function calls) are not supported; use
   :meth:`~nextorm.query.QuerySet.filter` instead.
   Multi-level relation traversal (``p.rel.field == value``,
   ``p.rel1.rel2.field == value``, etc.) is supported and automatically
   generates the required SQL ``JOIN`` chain.

Ordering
--------

The primary form uses ``.asc()`` / ``.desc()`` on a column expression:

.. code-block:: python

   db.select(User).order_by(User.name.asc())
   db.select(User).order_by(User.age.desc(), User.name.asc())

A bare column expression is also accepted and is auto-wrapped as ``ASC``:

.. code-block:: python

   db.select(User).order_by(User.name)           # ASC
   db.select(User).order_by(User.name, User.age.desc())  # mixed

A single lambda can be passed instead.  The lambda receives an
:class:`~nextorm.query.EntityProxy` whose attribute accesses return
:class:`~nextorm.expr.ColumnExpr` objects — the same proxy used by
:meth:`~nextorm.query.QuerySet.where`, but here the lambda returns a column
expression (or ordering item) rather than a boolean predicate:

.. code-block:: python

   db.select(User).order_by(lambda u: u.name)           # ASC (auto-wrapped)
   db.select(User).order_by(lambda u: u.name.desc())    # DESC
   db.select(User).order_by(
       lambda u: (u.active.desc(), u.name.asc())        # multi-column via tuple
   )

The lambda proxy also supports **chained relation traversal** — each
intermediate step follows a ``Single`` relation and generates the required
``JOIN`` automatically:

.. code-block:: python

   # Order cart items by shop slug → product name → SKU
   # Three JOINs are emitted: cartitem→productvariation, →product, →shop
   db.select(CartItem).order_by(
       lambda ci: (
           ci.product_variation.product.shop.slug,
           ci.product_variation.product.name,
           ci.product_variation.sku,
       )
   )

   # Two-level chain: order posts by their author's name
   db.select(Post).order_by(lambda p: p.author.name)

Repeating the same relation in multiple tuple entries is safe — the JOIN is
de-duplicated automatically.

Each call to ``order_by()`` **replaces** any previous ordering.

Limit and offset
----------------

.. code-block:: python

   db.select(Product).limit(10)
   db.select(Product).offset(20).limit(10)   # rows 21-30

Slicing and indexing
~~~~~~~~~~~~~~~~~~~~

:class:`~nextorm.query.QuerySet` supports Python slice and index syntax:

.. code-block:: python

   first  = db.select(Product)[0]             # single row
   page   = db.select(Product)[10:20]         # list of rows

Pagination
~~~~~~~~~~

For human-readable 1-based pagination:

.. code-block:: python

   page2 = db.select(Product).order_by(Product.name).page(2, pagesize=20)

Terminal methods
----------------

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Method
     - Description
   * - ``fetch_all()``
     - Return all matching rows as a list.
   * - ``fetch_one()``
     - Return the first matching row or ``None``.
   * - ``first()``
     - Alias for ``fetch_one()``.
   * - ``get()``
     - Return one row or ``None``; raises if multiple rows match.
   * - ``get_or_raise()``
     - Like ``get()`` but raises :exc:`~nextorm.exceptions.ObjectNotFound` when
       no row matches.
   * - ``count()``
     - Return ``COUNT(*)``.
   * - ``exists()``
     - Return ``True`` / ``False``.
   * - ``delete()``
     - Issue ``DELETE … WHERE …``; returns affected row count.
   * - ``update(**values)``
     - Issue ``UPDATE … SET … WHERE …``; returns affected row count.

Aggregation
-----------

Use the aggregate terminal methods or the module-level functions from
:mod:`nextorm.generators`:

.. code-block:: python

   from nextorm import count, sum, avg, min, max

   total = db.select(Product).count()
   total_value = db.select(Product).sum("price")
   avg_price   = db.select(Product).avg("price")
   cheapest    = db.select(Product).min("price")
   priciest    = db.select(Product).max("price")

   # Generator-expression form
   n = count(u for u in User if u.active == True)

.. code-block:: python

   # Concatenate all names as a string
   names = db.select(User).group_concat("name", sep=", ")

Random rows
-----------

.. code-block:: python

   sample = db.select(Product).random(5)

Joins
-----

.. code-block:: python

   db.select(Post).join(
       Comment, Comment.post_id == Post.id, join_type="LEFT"
   ).fetch_all()

Eager loading (prefetch)
------------------------

Avoid N+1 queries by declaring which relations to eager-load:

.. code-block:: python

   posts = db.select(Post).prefetch(Post.comments, Post.author).fetch_all()
   for post in posts:
       # post.comments already loaded — no extra query
       print(post.author.name, len(post.comments))

Raw SQL
-------

For advanced queries that NextORM cannot express:

.. code-block:: python

   # Returns entity instances mapped from raw SQL
   users = db.select(User).raw(
       "SELECT * FROM user WHERE name ILIKE %s",
       params=["%alice%"],
   )

   # Or for one-row:
   user = db.select(User).raw_one("SELECT * FROM user WHERE id = ?", [1])

Debug helpers
-------------

Inspect the generated SQL without executing the query:

.. code-block:: python

   qs = db.select(User).filter(User.age >= 18).order_by(User.name)
   print(qs.get_sql())
   # → SELECT id, name, age FROM "user" WHERE age >= ? ORDER BY name ASC

   qs.show()           # formatted tabular output to stdout

Distinct
--------

.. code-block:: python

   db.select(User).distinct().fetch_all()

FOR UPDATE
----------

Lock rows for update (PostgreSQL / MariaDB only):

.. code-block:: python

   user = db.select(User).filter(User.id == 1).for_update().fetch_one()
   user = db.select(User).filter(User.id == 1).for_update(skip_locked=True).fetch_one()

Async queries
-------------

Use :meth:`~nextorm.async_database.AsyncDatabase.aselect` for the async variant.
The :class:`~nextorm.async_database.AsyncQuerySet` API is identical except every
terminal method is a coroutine:

.. code-block:: python

   results = await db.aselect(User).filter(User.age >= 18).fetch_all()
   count   = await db.aselect(User).count()
