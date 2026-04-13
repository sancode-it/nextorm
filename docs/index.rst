NextORM
=======

.. image:: https://img.shields.io/pypi/v/nextorm
   :alt: PyPI version
   :target: https://pypi.org/project/nextorm/

.. image:: https://img.shields.io/pypi/pyversions/nextorm
   :alt: Python versions

.. image:: https://img.shields.io/badge/license-Apache%202.0-blue
   :alt: License

----

**NextORM** is a modern, async-capable Python ORM — a fully-typed successor to PonyORM.
It brings the expressive PonyORM query style into the era of Python 3.12+ type annotations,
``asyncio``, and first-class migration tooling.

.. grid:: 3
   :gutter: 2

   .. grid-item-card:: :octicon:`zap` Async-ready
      :text-align: center

      Every operation has both a sync and ``async`` counterpart. NextORM auto-detects
      the event loop — no configuration required.

   .. grid-item-card:: :octicon:`check-circle` Fully typed
      :text-align: center

      Field types are real Python generics (``Req[str]``, ``PK[int]``).
      Pyright and mypy understand them without plugins.

   .. grid-item-card:: :octicon:`database` Migrations built-in
      :text-align: center

      Schema diffing, file-based migrations, and a CLI — no third-party
      migration framework needed.

.. code-block:: python

   from nextorm import Database, Entity, PK, Req, Opt, Set, db_session

   class Tag(Entity):
       name: Req[str]

   class Product(Entity):
       name:  Req[str]
       price: Req[float]
       desc:  Opt[str]
       tags:  Set["Tag"]

   db = Database(entities=[Tag, Product])
   db.bind("sqlite", ":memory:")
   db.generate_mapping(create_tables=True)

   with db_session:
       p = Product(name="Widget", price=9.99)
       p.tags.add(Tag(name="new"))

   products = db.select(Product).filter(Product.price < 20).fetch_all()

----

.. toctree::
   :maxdepth: 2
   :caption: User guide

   getting_started
   entities
   queries
   sessions
   relations
   async
   migrations
   pooling
   debugging

.. toctree::
   :maxdepth: 1
   :caption: Reference

   api/index
   changelog

.. toctree::
   :maxdepth: 1
   :caption: Internals

   internals

.. toctree::
   :maxdepth: 1
   :caption: Migration guide

   ponyorm
