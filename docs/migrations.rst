Migrations
==========

NextORM includes a built-in, file-based migration system.  Migrations are plain
Python modules with an ``upgrade(db)`` function.  A schema snapshot is kept in
``migrations/.schema_snapshot.json`` so NextORM can diff the current entity
schema against the last recorded state.

Workflow
--------

.. code-block:: text

   # 1. Generate a migration file from the current entity schema diff
   python -m nextorm.migrations.cli makemigrations --module myapp.models

   # 2. Review and edit the generated migration if needed
   # 3. Apply all pending migrations
   python -m nextorm.migrations.cli migrate --module myapp.models

   # 4. Check which migrations have been applied
   python -m nextorm.migrations.cli showmigrations --module myapp.models

Or use the PDM shorthand (if configured in your pyproject.toml):

.. code-block:: bash

   pdm run migrate --migrate

Migration files
---------------

Each migration file is a numbered Python module in the migrations directory:

.. code-block:: text

   migrations/
       .schema_snapshot.json       # auto-managed snapshot, commit this file
       0001_initial.py
       0002_add_email_to_user.py
       0003_create_tags_table.py

Example file:

.. code-block:: python

   # migrations/0002_add_email_to_user.py

   def upgrade(db):
       db._execute_dml(
           "ALTER TABLE user ADD COLUMN email TEXT NOT NULL DEFAULT ''", []
       )
       db._execute_dml(
           "ALTER TABLE user ALTER COLUMN email DROP DEFAULT", []
       )

   def downgrade(db):   # optional
       db._execute_dml("ALTER TABLE user DROP COLUMN email", [])

The ``db`` argument is the :class:`~nextorm.database.Database` instance passed
by the runner.

CLI reference
-------------

All commands require ``--module`` (the Python module that defines and binds the
:class:`~nextorm.database.Database` object) and accept ``--db-attr`` (attribute
name in the module, default ``db``) and ``--directory`` (default
``migrations/``).

.. code-block:: text

   python -m nextorm.migrations.cli makemigrations --module myapp.models [--db-attr db] [--directory PATH] [--name LABEL]
   python -m nextorm.migrations.cli migrate        --module myapp.models [--db-attr db] [--directory PATH] [--fake]
   python -m nextorm.migrations.cli showmigrations --module myapp.models [--db-attr db] [--directory PATH]

``makemigrations``
~~~~~~~~~~~~~~~~~~

Compares the current entity schema to the stored snapshot and writes a new
migration file if there are any differences.  Pass ``--name`` to give the file
a descriptive suffix:

.. code-block:: bash

   python -m nextorm.migrations.cli makemigrations --name add_post_status

If nothing has changed, the command exits without creating a file.

``migrate``
~~~~~~~~~~~

Applies every migration that has not yet been recorded in the
``_nextorm_migrations`` tracking table.  Migrations are applied in
ascending-number order.

The ``--fake`` flag records migrations as applied without executing the SQL —
useful when adopting an existing schema:

.. code-block:: bash

   python -m nextorm.migrations.cli migrate --fake

``showmigrations``
~~~~~~~~~~~~~~~~~~

Lists all migration files with their application status:

.. code-block:: text

   [x] 0001_initial.py          (2025-03-10T14:22:00)
   [x] 0002_add_email_to_user.py (2025-03-15T09:11:42)
   [ ] 0003_create_tags_table.py

Python API
----------

All CLI operations are available programmatically:

.. code-block:: python

   from nextorm import Database, migrate, makemigrations, MigrationRunner

   db = Database(entities=[User, Post])
   db.bind("sqlite", "app.db")
   db.generate_mapping()

   # Generate a migration file
   makemigrations(db, directory="migrations/", name="initial")

   # Apply pending migrations
   migrate(db, directory="migrations/")

   # Using the runner object for fine-grained control
   runner = MigrationRunner(db, directory="migrations/")
   statuses = runner.showmigrations()
   runner.migrate()

Tracking table
--------------

Applied migrations are recorded in ``_nextorm_migrations`` — a table that NextORM
creates automatically.  Each row stores the migration version and a timestamp.
Do not delete this table while you have applied migrations in production.

Schema snapshot
---------------

The file ``migrations/.schema_snapshot.json`` stores a JSON snapshot of the
entity schema as it existed when the last migration was generated.
**Commit this file to version control** — it is the baseline for future diffs.

Writing migration SQL
---------------------

Migrations use raw SQL for schema changes so you have full control:

.. code-block:: python

   def upgrade(db):
       # Add a column with a temporary default so existing rows are valid
       db._execute_dml(
           "ALTER TABLE post ADD COLUMN slug VARCHAR(255) NOT NULL DEFAULT ''", []
       )
       # Remove the default so future INSERTs must provide the value
       db._execute_dml(
           "ALTER TABLE post ALTER COLUMN slug DROP DEFAULT", []
       )

.. tip::

   For optional string columns (``Opt[str]``), no DEFAULT is needed for new
   tables because NextORM always provides an empty string.  Only use the
   ``NOT NULL DEFAULT ''/DROP DEFAULT`` pattern when *adding a column to an
   existing table*.
