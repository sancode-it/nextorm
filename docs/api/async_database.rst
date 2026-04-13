AsyncDatabase
=============

.. autoclass:: nextorm.async_database.AsyncDatabase
   :members:
   :undoc-members: False
   :show-inheritance:
   :member-order: bysource
   :exclude-members: __weakref__, __dict__

.. autoclass:: nextorm.async_database.AsyncQuerySet
   :members:
   :undoc-members: False
   :show-inheritance:
   :member-order: bysource
   :exclude-members: __weakref__, __dict__

.. rubric:: Public API summary

.. autosummary::

   nextorm.async_database.AsyncDatabase.bind
   nextorm.async_database.AsyncDatabase.register
   nextorm.async_database.AsyncDatabase.generate_mapping
   nextorm.async_database.AsyncDatabase.aselect
   nextorm.async_database.AsyncDatabase.asave
   nextorm.async_database.AsyncDatabase.ainsert
   nextorm.async_database.AsyncDatabase.adelete_instance
   nextorm.async_database.AsyncDatabase.acommit
   nextorm.async_database.AsyncDatabase.arollback
   nextorm.async_database.AsyncDatabase.aflush
   nextorm.async_database.AsyncDatabase.execute
   nextorm.async_database.AsyncDatabase.select_raw
   nextorm.async_database.AsyncDatabase.close
   nextorm.async_database.AsyncDatabase.schema
   nextorm.async_database.AsyncDatabase.is_bound
   nextorm.async_database.AsyncDatabase.last_sql
   nextorm.async_database.AsyncDatabase.local_stats
   nextorm.async_database.AsyncDatabase.clear_local_stats
   nextorm.async_database.AsyncDatabase.merge_local_stats
   nextorm.async_database.AsyncDatabase.get_connection
