Defining Entities
=================

All persistent objects in NextORM are subclasses of :class:`~nextorm.entity.Entity`.
The metaclass :class:`~nextorm.entity.EntityMeta` processes type annotations at class
definition time, so no runtime magic is required — just annotate and go.

Entity registration
-------------------

Every :class:`~nextorm.entity.Entity` subclass registers itself in a **global
registry** the moment its class body is executed (i.e. when the module is
imported). You never have to pass entities to a database explicitly — unless
you want to.

**Single database, auto-discovery** (most common):

.. code-block:: python

   # models.py  — entities register automatically on import
   class User(Entity):
       name: Req[str]

   class Post(Entity):
       title: Req[str]

   # app.py
   import models  # ensures models.py is executed before generate_mapping
   db = Database()
   db.bind("sqlite", "app.db")
   db.generate_mapping(create_tables=True)  # discovers User and Post automatically

**Scoped database** — pass an explicit list so only these entities are mapped:

.. code-block:: python

   db = Database(entities=[User, Post])   # only User and Post, nothing else
   db.bind("sqlite", ":memory:")
   db.generate_mapping(create_tables=True)

This is useful when multiple :class:`~nextorm.database.Database` instances
point to different databases and each should own only a subset of entities.

**Adding entities after construction** with :meth:`~nextorm.database.Database.register`:

.. code-block:: python

   db = Database(entities=[User])
   db.register(Post, Comment)    # added to db's scope after the fact
   db.bind("sqlite", ":memory:")
   db.generate_mapping(create_tables=True)

.. note::
   - ``register()`` accepts one or more entity classes as positional arguments.
   - Calling ``register()`` on an entity that is already in the list is a no-op.
   - If you never pass ``entities=`` and never call ``register()``, the database
     will discover **all** globally registered entities.  This auto-discovery is
     convenient in single-database apps but can be surprising when two
     :class:`~nextorm.database.Database` instances exist — both would try to map
     all entities.  Use ``entities=[...]`` or ``register()`` to partition them.

The :attr:`~nextorm.database.Database.entities` property returns the current
mapping as a ``{name: class}`` dict:

.. code-block:: python

   for name, cls in db.entities.items():
       print(name, cls._table_name_)

Field types
-----------

There are four core field markers:

.. list-table::
   :header-rows: 1
   :widths: 20 50 30

   * - Annotation
     - Meaning
     - SQL
   * - ``PK[T]``
     - Primary key, auto-generated (int by default)
     - ``INTEGER PRIMARY KEY AUTOINCREMENT``
   * - ``Req[T]``
     - Required — not nullable
     - ``NOT NULL``
   * - ``Opt[T]``
     - Optional — may be ``None``
     - nullable column
   * - ``Local[T]``
     - Local / transient — never persisted
     - (no column)

.. code-block:: python

   from nextorm import Entity, PK, Req, Opt, Local

   class Product(Entity):
       id:          PK[int]
       name:        Req[str]
       price:       Req[float]
       description: Opt[str]
       _cache:      Local[dict]   # in-memory only

Python types map to SQL column types as follows:

.. list-table::
   :header-rows: 1
   :widths: 25 25 25 25

   * - Python type
     - SQLite
     - PostgreSQL
     - MariaDB
   * - ``int``
     - ``INTEGER``
     - ``INTEGER``
     - ``INT``
   * - ``str``
     - ``TEXT``
     - ``VARCHAR(255)``
     - ``VARCHAR(255)``
   * - ``float``
     - ``REAL``
     - ``DOUBLE PRECISION``
     - ``DOUBLE``
   * - ``bool``
     - ``INTEGER``
     - ``BOOLEAN``
     - ``TINYINT(1)``
   * - ``datetime``
     - ``TEXT``
     - ``TIMESTAMP``
     - ``DATETIME``
   * - ``Decimal``
     - ``TEXT``
     - ``NUMERIC``
     - ``DECIMAL``
   * - ``bytes``
     - ``BLOB``
     - ``BYTEA``
     - ``BLOB``

Primary keys
------------

Integer auto-increment (default)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   class Article(Entity):
       id: PK[int]      # AUTOINCREMENT integer, assigned by the DB

String primary keys
~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   class Country(Entity):
       code: PK[str]    # must be set manually before saving

UUID / ULID primary keys
~~~~~~~~~~~~~~~~~~~~~~~~

NextORM can auto-generate time-ordered UUIDs (v7), random UUIDs (v4), or ULIDs.
Import the sentinel types:

.. code-block:: python

   from nextorm import Entity, PK
   from nextorm.fields import uuid7, uuid4, ulid

   class Event(Entity):
       id: PK[uuid7]    # time-ordered UUID, auto-generated before INSERT

   class File(Entity):
       id: PK[uuid4]    # random UUID, auto-generated before INSERT

   class Log(Entity):
       id: PK[ulid]     # 26-char ULID string, auto-generated before INSERT

Composite primary keys
~~~~~~~~~~~~~~~~~~~~~~

Use the :func:`~nextorm.fields.PrimaryKey` helper as a class attribute:

.. code-block:: python

   from nextorm import Entity, Req
   from nextorm.fields import PrimaryKey

   class OrderLine(Entity):
       order_id:   Req[int]
       product_id: Req[int]
       quantity:   Req[int]

       pk = PrimaryKey("order_id", "product_id")

Default values
--------------

Assign a plain value or a callable as the default by calling the marker with
the ``default=`` option:

.. code-block:: python

   from datetime import datetime
   from nextorm import Entity, Req

   class Comment(Entity):
       text:       Req[str]
       created_at: Req[datetime] = Req(default=datetime.utcnow)
       score:      Req[int]      = Req(default=0)

.. note::

   Callables (like ``datetime.utcnow``) are invoked fresh for each new instance.
   Plain values are used as-is; avoid mutable defaults like ``[]`` — use
   a factory callable instead.

Custom column names
-------------------

By default the column name is the attribute name. Override it with the
``column=`` option on the marker:

.. code-block:: python

   class User(Entity):
       first_name: Req[str] = Req(column="fname")

Custom table names
------------------

By default the table name is the class name lower-cased. Override with
``_table_name_``:

.. code-block:: python

   class UserAccount(Entity):
       _table_name_ = "user_account"
       id: PK[int]
       email: Req[str]

Unique constraints and indexes
-------------------------------

Single-column unique constraint:

.. code-block:: python

   class User(Entity):
       email: Req[str] = Req(unique=True)

Multi-column constraints use the helpers at class level:

.. code-block:: python

   from nextorm.fields import composite_key, composite_index

   class Booking(Entity):
       user_id:  Req[int]
       event_id: Req[int]
       seat:     Req[str]

       # unique (user_id, event_id, seat) combination
       _unq_user_event_seat = composite_key("user_id", "event_id", "seat")
       # non-unique index on (user_id, event_id) for fast lookups
       _idx_user_event = composite_index("user_id", "event_id")

**Constraint assignment naming** — The class-attribute name (``_unq_*``,
``_idx_*``) is used only for internal metaclass discovery and developer
clarity. The name must:

- Start with a prefix: ``_unq_`` for unique keys, ``_idx_`` for indexes, ``_pk_`` for primary keys
- Follow with the involved attribute names joined by underscores
- Be a valid Python identifier (lowercase, alphanumeric + underscores)
- Be unique within the class if declaring multiple constraints

Examples:

.. code-block:: python

   class Order(Entity):
       customer_id: Req[int]
       date: Req[datetime]
       status: Req[str]
       
       # Composite primary key on two foreign keys
       _pk_ = PrimaryKey("customer", "date")
       # Unique constraint
       _ck_customer_date = composite_key("customer_id", "date")
       # Index for range queries
       _idx_date_status = composite_index("date", "status")

**Automatic constraint naming** — The database constraint and index names are
auto-generated from the table name and column names; they follow these patterns:

.. code-block:: python

   fk_<table>__<column>          # Foreign key
   unq_<table>__<col1>__<col2>   # Unique constraint (multi-column)
   idx_<table>__<col1>__<col2>   # Non-unique index (multi-column)
   idx_<table>__<column>         # Single-column index

**Override FK names only** — You can explicitly override foreign key constraint
names via the ``fk_name`` option on ``Single`` relations:

.. code-block:: python

   class Comment(Entity):
       # Auto-generated name: fk_comment__post_id
       post: Single[Post]
       
       # Custom FK name
       author: Single[User] = Single(fk_name="fk_comment_author")

Composite constraint names (from ``PrimaryKey``, ``composite_key``,
``composite_index``) and single-column index names are always auto-generated
— they cannot be overridden.

Special column types
--------------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Sentinel
     - Description
   * - ``LongStr``
     - Large text; lazy-loaded by default (``LONGTEXT`` / ``TEXT``)
   * - ``Json``
     - JSON/JSONB column; Python dict/list auto-serialised
   * - ``DateTimeTz``
     - Timezone-aware datetime (``TIMESTAMPTZ`` on PostgreSQL)
   * - ``Vec[n]``
     - Fixed-dimension vector (pgvector on PostgreSQL)
   * - ``uuid7``, ``uuid4``, ``ulid``
     - Auto-generated UUID/ULID primary keys (see above)

.. code-block:: python

   from nextorm.fields import LongStr, Json, DateTimeTz, Vec

   class Article(Entity):
       body:      Req[LongStr]       # loaded lazily
       metadata:  Opt[Json]
       published: Opt[DateTimeTz]
       embedding: Req[Vec[384]]      # pgvector column

Local (transient) fields
------------------------

Use :class:`~nextorm.fields.Local[T]` to attach computed or cached state to an instance.
Local fields are **never** written to or read from the database — they live in
``instance.__dict__`` only.  They are safe to initialise in lifecycle hooks:

.. code-block:: python

   from nextorm import Entity, Req, Local

   class User(Entity):
       name: Req[str]
       _full_name: Local[str]           # computed value
       _cache: Local[dict] = Local[dict](default=dict)  # factory default

   user = User(name="Alice")
   user._full_name = "Dr. Alice"  # set manually
   user._cache["key"] = "value"

**Options** (passed as ``Local[T](…)``):

- ``default``: Value or callable; invoked on ``Entity()`` construction
- ``py_check``: Callable that validates the value; receives the value, should raise or return falsy on failure

.. code-block:: python

   class Task(Entity):
       priority: Req[int]
       _validated_priority: Local[int] = Local[int](py_check=lambda x: 1 <= x <= 5)

Lifetime hooks
--------------

Override these methods on your entity to react to persistence events:

.. code-block:: python

   class Order(Entity):
       total: Req[float]
       confirmed: Req[bool]

       def before_insert(self) -> None:
           """Called just before the row is INSERTed for the first time."""
           self.confirmed = False

       def after_load(self) -> None:
           """Called after a row is SELECTed and mapped to this instance."""
           ...

Available hooks: ``after_load``, ``before_insert``, ``after_insert``,
``before_update``, ``after_update``, ``before_delete``, ``after_delete``.

Single-table inheritance
------------------------

Subclass an entity to create an STI hierarchy. NextORM stores all subclasses
in a single table and uses a discriminator column to distinguish them:

.. code-block:: python

   class Animal(Entity):
       _discriminator_col_ = "kind"
       name: Req[str]

   class Dog(Animal):
       _discriminator_val_ = "dog"
       breed: Req[str]

   class Cat(Animal):
       _discriminator_val_ = "cat"
       indoor: Req[bool]

When you ``SELECT`` from the parent class all subclass instances are returned
with their full type.

Enum fields
-----------

Python :class:`enum.Enum` values are stored as their underlying value (``str``
or ``int``). No extra configuration is needed — the ORM serialises and
deserialises automatically:

.. code-block:: python

   import enum

   class Status(enum.StrEnum):
       DRAFT = "draft"
       PUBLISHED = "published"

   class Post(Entity):
       title:  Req[str]
       status: Req[Status]

---

Working with entity instances
------------------------------

Creating and saving
~~~~~~~~~~~~~~~~~~~~

Construct an entity inside a :func:`~nextorm.session.db_session` block.
NextORM tracks the new object automatically and inserts it on commit — no
explicit save call required:

.. code-block:: python

   from nextorm import db_session

   with db_session:
       user = User(name="alice", age=30)
   # ← INSERT fires here; user.id is now assigned by the DB

If you need the auto-generated PK *before* the session ends you can flush
at any granularity:

.. code-block:: python

   from nextorm import flush, db_session

   with db_session:
       user = User(name="alice", age=30)
       flush()                 # flush *all* pending objects in the session
       print(user.id)          # available here

For a single entity use :meth:`~nextorm.entity.Entity.flush` — it saves only
that one instance without touching the rest of the session:

.. code-block:: python

   with db_session:
       order = Order(ref="ORD-001")
       other = Order(ref="ORD-002")   # not yet flushed
       order.flush()                  # only this order is written
       print(order.id)                # PK assigned immediately

To also commit the transaction (making the change durable) use
:meth:`~nextorm.entity.Entity.commit`:

.. code-block:: python

   with db_session:
       order = Order(ref="ORD-001")
       order.commit()          # flush this entity + commit the transaction

:meth:`~nextorm.database.Database.save` is available for fine-grained, explicit
control (e.g. outside a session or when you need the PK back immediately without
a full flush).  Use :meth:`~nextorm.database.Database.insert` to force an
unconditional ``INSERT`` — useful when you set the PK yourself:

.. code-block:: python

   with db_session:
       item = Product(name="Widget")
       item.id = 999           # explicit PK to preserve on import
       db.insert(item)         # always INSERT, never UPDATE

Updating
~~~~~~~~

Assign new values directly or use :meth:`~nextorm.entity.Entity.set` for
bulk updates:

.. code-block:: python

   with db_session:
       user = db.select(User).filter(User.id == 1).get()
       user.name = "bob"       # dirty-tracked, flushed on commit

   with db_session:
       user.set(name="carol", age=25)   # equivalent to two setattr calls

Deleting
~~~~~~~~~

Either call the database method or the shortcut on the instance:

.. code-block:: python

   with db_session:
       db.delete_instance(user)    # explicit: pass db + entity

   with db_session:
       user.delete()               # shortcut: entity must have been loaded by a db

.. note::
   ``entity.delete()`` requires that the entity was fetched or saved through a
   database session so the ``_db_`` context attribute is set.  If you hold a
   bare constructed object, use ``db.delete_instance(entity)`` directly.

Table management
~~~~~~~~~~~~~~~~~

NextORM exposes helpers for dropping and (re-)creating tables without writing
raw DDL.  These are primarily useful for test tear-down and manual database
maintenance.

**Drop a single entity's table:**

.. code-block:: python

   # Raises RuntimeError if the table is not empty (default)
   User.drop_table()

   # Force-drop even if rows exist
   User.drop_table(with_all_data=True)

**Drop all tables mapped to a database:**

.. code-block:: python

   db.drop_all_tables(with_all_data=True)   # useful for test cleanup

**Drop a table by name** (useful for join / auxiliary tables):

.. code-block:: python

   db.drop_table("user_tags", if_exists=True, with_all_data=True)

**Drop the M2M join table via the collection attribute:**

.. code-block:: python

   with db_session:
       post.tags.drop_table(with_all_data=True)   # M2M only — O2M raises RuntimeError

**Re-create all entity tables** after they have been dropped:

.. code-block:: python

   db.drop_all_tables(with_all_data=True)
   db.create_tables()    # idempotent — wraps CREATE TABLE IF NOT EXISTS

**Disconnect the connection pool** (e.g. before deleting a SQLite file):

.. code-block:: python

   db.disconnect()       # closes all idle connections in the pool
   os.remove("app.db")

.. warning::
   ``drop_table`` and ``drop_all_tables`` are **irreversible** destructive
   operations.  Always confirm ``with_all_data=True`` intentionally or check
   that the table is empty first by relying on the default ``with_all_data=False``
   guard, which raises :exc:`RuntimeError` when rows are present.

Looking up by primary key
~~~~~~~~~~~~~~~~~~~~~~~~~~

Use the subscript shortcut or the query API interchangeably:

.. code-block:: python

   # Single-column PK
   user = User[1]              # raises KeyError when not found

   # Composite PK — list values in declaration order
   line = OrderLine[order_id, product_id]

   # Equivalent via QuerySet
   user = db.select(User).filter(User.id == 1).get()

Class-level lookups
~~~~~~~~~~~~~~~~~~~~

:meth:`~nextorm.entity.Entity.get`, :meth:`~nextorm.entity.Entity.exists`,
and :meth:`~nextorm.entity.Entity.select` match against one or more field
values without explicitly passing a database.  All three accept the same
uniform calling convention:

``method(predicate=None, *conditions: SqlNode, **kwargs)``

All arguments are optional and can be freely combined:

**No arguments** — return all rows (``select`` only):

.. code-block:: python

   users = User.select().order_by(User.name).fetch_all()

**Keyword arguments** — simple column equality filters:

.. code-block:: python

   user = User.get(email="alice@example.com")     # None if not found
   if User.exists(email="alice@example.com"):
       ...
   admins = User.select(role="admin").fetch_all()

**Positional SqlNode conditions** — direct column expressions:

.. code-block:: python

   user = User.get(User.age > 18)
   adults = User.select(User.age >= 18).fetch_all()

**Lambda predicate** — more expressive conditions using the same
bytecode-decompiler that powers generator-expression queries:

.. code-block:: python

   user = User.get(lambda u: u.age > 18)
   if User.exists(lambda u: u.active == True):
       ...
   active = User.select(lambda u: u.active).fetch_all()

**All combined** — predicate, conditions, and kwargs together:

.. code-block:: python

   user = User.get(lambda u: u.age >= 18, User.active == True, role="admin")

Closure variables (values captured from the enclosing scope) are resolved
automatically:

.. code-block:: python

   min_age = 18
   user = User.get(lambda u: u.age >= min_age)

The async variants :meth:`~nextorm.entity.Entity.aget` and
:meth:`~nextorm.entity.Entity.aselect` support the same calling convention:

.. code-block:: python

   user   = await User.aget(lambda u: u.email == email)
   admins = await User.aselect(role="admin").fetch_all()
   count  = await User.aselect(User.active == True).count()

.. note::
   Multi-level attribute access across relations (e.g.
   ``lambda c: c.shop.slug == slug``) automatically generates the required
   SQL ``JOIN`` chain.  There is no hard limit on chain depth.

.. warning::
   These methods use :func:`~nextorm.entity._find_db_for_entity` to locate
   a bound database automatically. When more than one database is bound to the
   entity, pass the query through ``db.select(Entity)`` instead to be explicit
   about which database to use.

Inspecting the primary key
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   pk = user.get_pk()          # returns the PK value (or tuple for composite)

Serialising to a dictionary
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   user.to_dict()
   # → {"id": 1, "name": "alice", "age": 30}

   user.to_dict(exclude=["id"])
   # → {"name": "alice", "age": 30}

   # Include prefetched collections as nested lists
   post.to_dict(with_collections=True)
   # → {"id": 1, "title": "Hello", "comments": [{"id": 5, "text": "Great!"}]}

   # Include loaded Single relations as nested dicts
   comment.to_dict(related_objects=True)
   # → {"id": 1, "text": "Nice", "author": {"id": 2, "name": "bob"}}

Options:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Parameter
     - Description
   * - ``only=[...]``
     - Include only these field names.
   * - ``exclude=[...]``
     - Exclude these field names (applied after ``only``).
   * - ``with_collections=True``
     - Include ``Set[T]`` relations as nested lists (must be prefetched first).
   * - ``with_lazy=True``
     - Include lazy fields that have not yet been loaded (triggers a SELECT).
   * - ``related_objects=True``
     - Include loaded ``Single[T]`` relations as nested dicts.

Raw SQL class methods
~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   # Sync
   users = User.select_by_sql(db, "SELECT * FROM user WHERE age > ?", [18])
   user  = User.get_by_sql(db, "SELECT * FROM user WHERE email = ?", ["a@b.com"])

   # Async
   users = await User.aselect_by_sql(db, "SELECT * FROM user WHERE age > %s", [18])
   user  = await User.aget_by_sql(db, "SELECT * FROM user WHERE email = %s", ["a@b.com"])

These are thin wrappers around ``db.select(Entity).raw(sql, params)`` and
``db.select(Entity).raw_one(sql, params)``.

Positional argument shorthands
------------------------------

For the most common type-specific options, scalar markers accept a positional
argument so you can skip the keyword name:

.. list-table::
   :header-rows: 1
   :widths: 40 30 30

   * - Full keyword form
     - Shorthand
     - Positional arg(s)
   * - ``Req[str](max_len=128)``
     - ``Req[str](128)``
     - ``max_len``
   * - ``Opt[str](max_len=64)``
     - ``Opt[str](64)``
     - ``max_len``
   * - ``Req[int](size=32)``
     - ``Req[int](32)``
     - ``size``
   * - ``Req[float](tolerance=0.01)``
     - ``Req[float](0.01)``
     - ``tolerance``
   * - ``Req[Decimal](precision=10, scale=2)``
     - ``Req[Decimal](10, 2)``
     - ``precision``, ``scale``
   * - ``Req[datetime](precision=3)``
     - ``Req[datetime](3)``
     - ``precision``
   * - ``Req[Vec](dimensions=384)``
     - ``Req[Vec](384)``
     - ``dimensions``
   * - ``Req[uuid7](uuid_auto="v7")``
     - ``Req[uuid7]("v7")``
     - ``uuid_auto``

Applies equally to ``Opt[T]``, ``PK[T]``, and ``Req[T]`` where the type
supports it (e.g. ``Opt[str](128)`` also works).

Field and Relation Marker Options
---------------------------------

Options for scalar fields (``PK``, ``Req``, ``Opt``):
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Column naming and SQL mapping**

* ``column``: str | None
    Override the database column name for this field (default: attribute name).
* ``sql_type``: str | None
    Override inferred SQL type string (e.g. "JSONB").
* ``sql_default``: str | None
    Raw SQL expression for DDL DEFAULT (e.g. "CURRENT_TIMESTAMP").

**Primary key and uniqueness**

* ``primary_key``: bool
    Mark as primary key (implied by PK[]).
* ``unique``: bool
    Add a unique constraint on this column.
* ``index``: bool
    Create a non-unique index on this column.
* ``auto``: bool
    Auto-incrementing integer (PK[int] only).
* ``uuid_auto``: str | None
    "v7", "v4", or "ulid" — Python-side UUID/ULID auto-generation.

**Nullability and defaults**

* ``nullable``: bool
    Allow NULL values (implied by Opt[]).
* ``default``: Any
    Python-side default value or factory (callable or value).

**Type-specific options**

* ``max_len``: int | None
    For str: Maximum string length.
* ``precision``: int | None
    For Decimal: total significant digits.
* ``scale``: int | None
    For Decimal: digits after decimal point.
* ``unsigned``: bool
    For int: UNSIGNED modifier (MariaDB only).
* ``size``: int | None
    For int: column bit width — 8, 16, 32, or 64.
* ``dimensions``: int | None
    For Vec: number of vector dimensions (e.g. 384, 1536).

**Validation and assignment**

* ``py_check``: Callable[[object], bool] | None
    Callable validator; receives value; raises/returns falsy on failure.
* ``autostrip``: bool
    Strip leading/trailing whitespace on string assignment.
* ``min``: Any
    Inclusive lower bound for numeric/comparable fields.
* ``max``: Any
    Inclusive upper bound for numeric/comparable fields.

**Other**

* ``lazy``: bool
    Deferred loading: field excluded from main SELECT; loaded on first access.
* ``volatile``: bool
    Excluded from UPDATE; value is set by a DB trigger.

Options for relations (``Single``, ``Set``):
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Nullability and constraints**

* ``nullable``: bool
    Allow NULL values (Single only).
* ``primary_key``: bool
    Mark as primary key (Single only).
* ``cascade_delete``: bool | None
    Override ON DELETE action (True=CASCADE, False=RESTRICT, None=auto).

**Foreign key and join table mapping**

* ``column``: str | None
    Override the database column name for the FK (Single only).
* ``columns``: list[str] | None
    Composite FK column names (Single only).
* ``fk_name``: str | None
    Override the foreign key constraint name (Single only).
* ``table``: str | None
    Override the join table name for M2M (Set only).
* ``reverse_column``: str | None
    Override the join table column name for the reverse side (Set only).
* ``reverse_columns``: list[str] | None
    Composite join table column names for the reverse side (Set only).

**Relation ownership**

* ``owner``: bool | None
    O2O only: explicit owning-side override (rarely needed; advanced usage).

**Reverse relation**

* ``reverse``: str | None
    Name of the reverse relation on the target entity.

Examples:

.. code-block:: python

   class Product(Entity):
       id:    PK[int]
       name:  Req[str](column="product_name", max_len=100, unique=True)
       # or using the positional shorthand:
       short: Req[str](128)          # max_len=128

   class Order(Entity):
       product: Single[Product] = Single(column="prod_id", fk_name="fk_order__prod_id", nullable=False)

   class Tag(Entity):
       products: Set[Product] = Set(reverse_column="tag_ref_id", table="product_tag_link")

   class CatalogProduct(Entity):
       tags: Set[Tag] = Set(reverse_column="product_ref_id")
