Session
=======

.. autodata:: nextorm.session.db_session
   :annotation: = DBSessionManager instance

.. autodata:: nextorm.session.async_db_session
   :annotation: = alias for db_session

.. autoclass:: nextorm.session.DBSessionManager
   :members:
   :undoc-members: False
   :show-inheritance:
   :member-order: bysource
   :special-members: __enter__, __exit__, __aenter__, __aexit__, __call__

.. autoclass:: nextorm.session.SessionCache
   :members:
   :undoc-members: False
   :show-inheritance:
   :member-order: bysource

Module-level helpers
--------------------

.. autofunction:: nextorm.__init__.flush
.. autofunction:: nextorm.__init__.commit
.. autofunction:: nextorm.__init__.rollback
