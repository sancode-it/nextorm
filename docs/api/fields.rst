Fields
======

Field type markers
------------------

These are Python 3.12 generic classes that serve as type annotations for entity fields.
Sphinx autodoc cannot fully process Python 3.12 ``class Cls[T]:`` syntax, so they are
described here manually.  See :doc:`../entities` for usage examples.

.. py:class:: nextorm.fields.PK[T]

   Primary-key field — auto-generated integer by default.

   On the class: returns a :class:`~nextorm.expr.ColumnExpr` for use in query predicates.
   On an instance: returns the ``T`` value.

   Example::

      class User(Entity):
          id: PK[int]         # auto-increment integer PK
          id: PK[uuid7]       # time-ordered UUID PK

.. py:class:: nextorm.fields.Req[T]

   Required (non-nullable) field.  Maps to a ``NOT NULL`` column.

   Example::

      class Product(Entity):
          name: Req[str]

.. py:class:: nextorm.fields.Opt[T]

   Optional (nullable) field — value may be ``None``.  Maps to a nullable column.

   Example::

      class Product(Entity):
          description: Opt[str]

.. py:class:: nextorm.fields.Local[T]

   Local (transient) field — never written to or read from the database.
   Use it to attach computed or cached state to an entity instance.

   Example::

      class User(Entity):
          _full_name: Local[str]
          _cache: Local[dict] = Local[dict](default=dict)

   **Options** (passed as ``Local[T](…)``):

   - ``default``: Value or callable returned on ``Entity()`` construction
   - ``py_check``: Callable that validates the value on assignment

.. py:class:: nextorm.fields.Set[T]

   Collection relation attribute — used for both one-to-many and many-to-many.

   Declare ``Set[Child]`` on the *one* side and ``Single[Parent]`` on the *many* side for
   one-to-many; declare ``Set[Other]`` on **both** entities for many-to-many.

   **Options** (passed as ``Set[T](…)``):

   - ``table``: (M2M only) Override join table name; defaults to ``{a}_{b}`` (alphabetical)
   - ``reverse_column``: (M2M only) Override the FK column name to this entity; defaults to ``{entity_name}_id``
   - ``reverse_columns``: (M2M only) Override the composite FK column names; overrides ``reverse_column``

.. py:class:: nextorm.fields.Single[T]

   Single-entity relation attribute (FK).

   Use ``Single[Other]`` for a required FK (NOT NULL, CASCADE).
   Use ``Single[Other | None]`` for an optional FK (NULLABLE, SET NULL).
   When both sides use ``Single``, a UNIQUE constraint is added (one-to-one).

   **Options** (passed as ``Single[T](…)``):

   - ``nullable``: Override the nullability (defaults to auto-detect from ``T | None``)
   - ``cascade_delete``: ``True`` for CASCADE, ``False`` for RESTRICT, ``None`` (default) for auto-derive from nullable
   - ``owner``: (one-to-one only) ``True`` to mark as owning side (has FK), ``False`` as non-owning, ``None`` (default) for auto-detect
   - ``fk_name``: Custom FK constraint name (defaults to auto-generated ``fk_…``)
   - ``column``: Override the FK column name; defaults to ``{relation_name}_id``
   - ``columns``: Override the composite FK column names; overrides ``column``

Special column types
--------------------

.. autoclass:: nextorm.fields.LongStr
.. autoclass:: nextorm.fields.Json
.. autoclass:: nextorm.fields.DateTimeTz
.. autoclass:: nextorm.fields.Vec
   :members: __class_getitem__

UUID / ULID types
-----------------

.. autoclass:: nextorm.fields.uuid7
.. autoclass:: nextorm.fields.uuid4
.. autoclass:: nextorm.fields.ulid
.. autoclass:: nextorm.fields.ULID

Metadata classes
----------------

:class:`~nextorm.fields.FieldSpec` and :class:`~nextorm.fields.RelationSpec`
are the internal dataclasses that hold the resolved field configuration after
:class:`~nextorm.entity.EntityMeta` processes the class body.  They are exposed
publicly for introspection (e.g. ``Entity._fields_["name"].spec``) but should
not be used directly as class-body values — use the marker-call syntax
(``Req(...)``, ``Opt(...)``, ``Single(...)``, etc.) instead.

.. autoclass:: nextorm.fields.FieldSpec
   :members:

.. autoclass:: nextorm.fields.RelationSpec
   :members:

.. autoclass:: nextorm.fields.LocalSpec
   :members:

Composite constraints
---------------------

.. autofunction:: nextorm.fields.composite_key
.. autofunction:: nextorm.fields.composite_index
.. autofunction:: nextorm.fields.PrimaryKey

.. autoclass:: nextorm.fields.CompositeConstraint
   :members:

