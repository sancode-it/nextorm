Database
========

.. autoclass:: nextorm.database.Database
   :members:
   :undoc-members: False
   :show-inheritance:
   :member-order: bysource
   :exclude-members: __weakref__, __dict__

.. rubric:: Public API summary

.. autosummary::

   nextorm.database.Database.bind
   nextorm.database.Database.unbind
   nextorm.database.Database.register
   nextorm.database.Database.generate_mapping
   nextorm.database.Database.select
   nextorm.database.Database.save
   nextorm.database.Database.insert
   nextorm.database.Database.delete_instance
   nextorm.database.Database.commit
   nextorm.database.Database.rollback
   nextorm.database.Database.flush
   nextorm.database.Database.execute
   nextorm.database.Database.select_raw
   nextorm.database.Database.get_ddl
   nextorm.database.Database.migrate
   nextorm.database.Database.close
   nextorm.database.Database.schema
   nextorm.database.Database.is_bound
   nextorm.database.Database.last_sql
   nextorm.database.Database.local_stats
   nextorm.database.Database.clear_local_stats
   nextorm.database.Database.merge_local_stats
   nextorm.database.Database.get_connection
   nextorm.database.Database.create_tables
   nextorm.database.Database.drop_table
   nextorm.database.Database.drop_all_tables
   nextorm.database.Database.disconnect
