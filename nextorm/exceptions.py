"""nextorm exception hierarchy."""

__all__ = [
    "MappingError",
    "ObjectNotFound",
    "MultipleObjectsFoundError",
    "ConstraintError",
    "TransactionError",
    "OptimisticCheckError",
    "CommitException",
    "PartialCommitException",
]


class MappingError(Exception):
    """Raised when entity relation wiring is invalid or ambiguous.

    Thrown by :meth:`~nextorm.database.Database.generate_mapping` when:

    * A ``Set[T]`` attribute has no matching back-reference on ``T``.
    * Multiple relations between the same two entities exist without an
      explicit ``reverse=`` attribute to resolve the ambiguity.
    """


class ObjectNotFound(Exception):
    """Raised by :meth:`~nextorm.query.QuerySet.get_or_raise` when no row matches.

    Example::

        user = db.select(User).filter(User.id == 99).get_or_raise()
        # → ObjectNotFound: User matching the given filter was not found.
    """


class MultipleObjectsFoundError(Exception):
    """Raised by :meth:`~nextorm.query.QuerySet.get` or
    :meth:`~nextorm.query.QuerySet.get_or_raise` when more than one row matches.

    Example::

        # Multiple users named "alice"
        user = db.select(User).filter(User.name == "alice").get()
        # → MultipleObjectsFoundError: get() returned more than one User.
    """


class ConstraintError(Exception):
    """Raised when a database constraint (UNIQUE, NOT NULL, FK) is violated.

    Wraps the underlying DBAPI ``IntegrityError`` so callers can catch a
    provider-independent exception type.
    """


class TransactionError(Exception):
    """Raised for transaction-level problems (deadlock, serialization failure,
    session-is-over, etc.)."""


class OptimisticCheckError(Exception):
    """Raised when an optimistic version check fails on UPDATE.

    A version check fails when the row's current version in the database no
    longer matches the version the entity was loaded with — meaning another
    transaction updated the row concurrently.

    Example::

        alice = db.select(Article).filter(Article.id == 1).fetch_one()
        # ... another transaction updates article 1 and bumps its version ...
        alice.title = "New title"
        db.save(alice)  # → OptimisticCheckError: concurrent update detected
    """


class CommitException(TransactionError):
    """Raised when the primary-database commit fails during a staged commit.

    The ``exceptions`` attribute contains the list of underlying exceptions
    that were collected (primary failure first, then any rollback failures).

    When this is raised all secondary databases have been rolled back.
    """

    def __init__(self, msg: str, exceptions: list[Exception]) -> None:
        super().__init__(msg)
        self.exceptions = exceptions


class PartialCommitException(TransactionError):
    """Raised when the primary database committed but one or more secondary databases failed.

    The primary database's work is durable; the secondary failures are
    collected in ``exceptions``.  Manual reconciliation may be required.
    """

    def __init__(self, msg: str, exceptions: list[Exception]) -> None:
        super().__init__(msg)
        self.exceptions = exceptions
