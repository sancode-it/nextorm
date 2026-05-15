Relations
=========

NextORM supports three relation kinds: **many-to-one / one-to-one** (``Single``),
**one-to-many** (``Set`` + ``Single``), and **many-to-many** (``Set`` on both ends).

Field markers
-------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Marker
     - Use
   * - ``Single[T]``
     - FK pointing to one instance of ``T`` (required — NOT NULL)
   * - ``Single[T | None]``
     - Nullable FK (optional — SET NULL on delete)
   * - ``Set[T]``
     - Collection of related ``T`` instances

Many-to-one
-----------

A classic foreign key — ``Comment`` belongs to one ``Post``:

.. code-block:: python

   from nextorm import Entity, PK, Req, Set, Single

   class Post(Entity):
       title:    Req[str]
       comments: Set["Comment"]      # back-reference

   class Comment(Entity):
       text: Req[str]
       post: Single[Post]            # FK → post.id  (required)

Nullable FK (optional parent):

.. code-block:: python

   class Comment(Entity):
       text:   Req[str]
       post:   Single[Post | None]   # FK nullable, SET NULL on post delete

One-to-one
----------

When **both** sides declare ``Single``, NextORM adds a ``UNIQUE`` constraint to the
FK column:

.. code-block:: python

   class User(Entity):
       name:    Req[str]
       profile: Single["Profile | None"]

   class Profile(Entity):
       bio:  Req[str]
       user: Single[User]            # UNIQUE FK → user

Many-to-many
------------

Declare ``Set[T]`` on **both** entities. NextORM infers a join table automatically:

.. code-block:: python

   class Tag(Entity):
       name:     Req[str]
       products: Set["Product"]

   class Product(Entity):
       name: Req[str]
       tags: Set[Tag]

The join table name is derived from the two entity names sorted alphabetically
(e.g. ``product_tag``). You can customise it with ``Set(table=...)``.

Accessing collections
---------------------

Accessing a ``Set[T]`` attribute returns a :class:`~nextorm.collection.RelatedCollection`
that queries on demand:

.. code-block:: python

   post = db.select(Post).filter(Post.id == 1).get()
   comments = post.comments          # RelatedCollection — lazy
   n = post.comments.count()         # SELECT COUNT(*)
   for c in post.comments:           # SELECT * WHERE post_id = 1
       print(c.text)

Collections have full query-builder support:

.. code-block:: python

   # Get the whole collection as a QuerySet (for chaining)
   qs = post.comments.select()

   # Filter with SqlNode conditions or keyword equality shortcuts
   active  = post.comments.filter(Comment.approved == True).fetch_all()
   active  = post.comments.filter(approved=True).fetch_all()

   # Filter with a lambda predicate
   hot = post.comments.where(lambda c: c.score > 100).fetch_all()

   # All combined in select()
   post.comments.select(
       lambda c: c.score > 5,
       Comment.approved == True,
       spam=False,
   ).fetch_all()

.. list-table:: Collection query methods
   :header-rows: 1
   :widths: 40 60

   * - Method
     - Description
   * - ``col.select()``
     - Unfiltered :class:`~nextorm.query.QuerySet` — use when you need the whole collection as a QuerySet
   * - ``col.select(predicate, *conditions, **kwargs)``
     - Unified entry point: predicate, SqlNode conditions, and/or equality kwargs — all combined with AND
   * - ``col.filter(*conditions, **kwargs)``
     - Filter by ``SqlNode`` conditions and/or keyword equality shortcuts
   * - ``col.where(lambda c: ...)``
     - Filter with a lambda predicate (bytecode decompilation)
   * - ``col.order_by(...)``
     - Ordered :class:`~nextorm.query.QuerySet`
   * - ``col.page(n, pagesize)``
     - Paginated :class:`~nextorm.query.QuerySet`
   * - ``col.count()``
     - ``SELECT COUNT(*)`` without loading objects
   * - ``col.is_empty()``
     - ``True`` when the collection has no items
   * - ``col.load()``
     - Eagerly load all items, return as ``list``

.. note::

   ``select()``, ``filter()``, and ``where()`` mirror the same-named methods on
   :class:`~nextorm.query.QuerySet` and :class:`~nextorm.entity.Entity`, so the
   usage pattern is consistent regardless of whether you are querying a top-level
   entity or a relation.

Mutating collections
--------------------

.. code-block:: python

   with db_session:
       post = db.select(Post).get()
       post.comments.add(Comment(text="Great post!"))
       post.comments.remove(old_comment)
       post.comments.clear()         # remove all links (many-to-many) or delete (one-to-many)

Navigating ``Single`` relations
--------------------------------

Accessing a ``Single`` attribute lazy-loads the related entity on the first access:

.. code-block:: python

   comment = db.select(Comment).filter(Comment.id == 5).get()
   post = comment.post               # SELECT * FROM post WHERE id = ? (lazy load)
   print(post.title)

Assigning a relation:

.. code-block:: python

   with db_session:
       comment.post = new_post       # updates the FK column

Eager loading (prefetch)
------------------------

The N+1 problem
~~~~~~~~~~~~~~~

A classic N+1 scenario arises when you load a list of objects and then access
a relation on each one inside a loop:

.. code-block:: python

   posts = db.select(Post).fetch_all()       # 1 query
   for post in posts:
       print(post.author.name)               # 1 query per post → N extra queries

With 100 posts that is 101 queries.  **PonyORM** solves this transparently by
batching the FK look-ups: on the first attribute access inside the loop it
detects that a batch of objects is being iterated and issues a single
``WHERE pk IN (...)`` query covering all of them, storing results in the
identity map.

**NextORM does not do this automatically.** Relation attributes that have not
been pre-loaded raise a lazy-load query each time they are accessed — the N+1
risk is real.  The explicit solution is ``prefetch``.

Using ``prefetch``
~~~~~~~~~~~~~~~~~~

Declare which relations to eager-load alongside the main query:

.. code-block:: python

   posts = db.select(Post).prefetch(Post.comments, Post.author).fetch_all()
   for post in posts:
       # post.comments and post.author are pre-populated — zero extra SQL
       print(post.author.name, post.comments.count())

NextORM issues **one additional query per prefetched relation** using a
``WHERE pk IN (...)`` batch load, then attaches the results to the parent
objects before returning them.  The number of SQL statements is always
``1 + len(prefetched_relations)`` regardless of the number of rows.

You can pass attribute references or attribute name strings:

.. code-block:: python

   posts = db.select(Post).prefetch("comments", "author").fetch_all()

Multi-level prefetch
~~~~~~~~~~~~~~~~~~~~~

Chain ``prefetch()`` calls (or pass multiple relations) to load nested
relations in one go:

.. code-block:: python

   # Load posts, their comments, and each comment's author
   posts = (
       db.select(Post)
       .prefetch(Post.comments)
       .prefetch(Comment.author)   # second call adds to the set
       .fetch_all()
   )

.. note::
   Multi-level prefetch is resolved left-to-right.  ``Comment.author`` is
   loaded from the comment set that was populated by the first prefetch.

Async prefetch
~~~~~~~~~~~~~~

The async API uses the same interface:

.. code-block:: python

   posts = await db.aselect(Post).prefetch(Post.comments).fetch_all()

When not to worry
~~~~~~~~~~~~~~~~~~

If you are fetching a **single** object (``get()``/``fetch_one()``) and
accessing only a few relations once, lazy loading is perfectly fine — one or
two extra queries per request is usually acceptable.  Use ``prefetch`` when
iterating over collections.

Custom FK column name
----------------------

Use the ``column=`` option on ``Single(...)`` to override the default
``<attr>_id`` column name:

.. code-block:: python

   class Comment(Entity):
       text: Req[str]
       post: Single[Post] = Single(column="fk_post")

Custom join-table name
-----------------------

.. code-block:: python

   class Product(Entity):
       name: Req[str]
       tags: Set[Tag] = Set(table="product_tags")

Reverse back-reference
-----------------------

When two entities have multiple relations to each other, specify the
``reverse=`` parameter to resolve the ambiguity:

.. code-block:: python

   class User(Entity):
       authored:  Set["Post"]
       edited:    Set["Post"]

   class Post(Entity):
       title:  Req[str]
       author: Single[User] = Single(reverse="authored")
       editor: Single[User] = Single(reverse="edited")

Customizing relations
---------------------

Pass keyword arguments directly to the marker to override default behaviour:

**Foreign key column name** (``Single`` only):

.. code-block:: python

   class Comment(Entity):
       text: Req[str]
       post: Single[Post] = Single(column="post_fk")  # Column named "post_fk" not "post_id"

**Join table name** (many-to-many only):

.. code-block:: python

   class Product(Entity):
       tags: Set[Tag] = Set(table="product_has_tag")

**Join table column names** (many-to-many only):

.. code-block:: python

   class Product(Entity):
       tags: Set[Tag] = Set(
           reverse_column="tag_id",  # Column pointing to Tag
           column="product_id"       # Column pointing to Product
       )

**Cascade delete behaviour**:

.. code-block:: python

   class Tenant(Entity):
       name: Req[str]

   class User(Entity):
       # Optional FK; normally SET NULL on delete. Force CASCADE instead:
       tenant: Single[Tenant | None] = Single(cascade_delete=True)

**One-to-one ownership** (both sides declare ``Single``):

.. code-block:: python

   class User(Entity):
       profile: Single["Profile"] = Single(owner=True)  # This side has the FK

   class Profile(Entity):
       user: Single[User]  # Back-reference; no FK column here

Schema generation
-----------------

Relation rules as applied by ``generate_mapping``:

- ``Single[T]`` (required) → ``FOREIGN KEY … REFERENCES … ON DELETE CASCADE``
- ``Single[T | None]`` (optional) → ``FOREIGN KEY … REFERENCES … ON DELETE SET NULL``
- ``Set[T]`` + ``Single[T]`` (one-to-many) → FK on the ``Single`` side
- ``Set[T]`` + ``Set[T]`` (many-to-many) → auto-generated join table with two ``ON DELETE CASCADE`` FKs
