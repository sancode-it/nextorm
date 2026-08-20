"""Tests for Database._validate_relations() and MappingError."""

from __future__ import annotations

import pytest

from nextorm.database import Database
from nextorm.entity import Entity
from nextorm.exceptions import (
    ConstraintError,
    MappingError,
    MultipleObjectsFoundError,
    ObjectNotFound,
    TransactionError,
)
from nextorm.fields import Req, Set, Single

# ---------------------------------------------------------------------------
# Valid entities — O2M (Set + ManyToOne back-ref)
# ---------------------------------------------------------------------------


class ValBlog(Entity):
    title: Req[str]
    posts: Set["ValBlogPost"]  # noqa: UP037


class ValBlogPost(Entity):
    body: Req[str]
    blog: Single[ValBlog]


# ---------------------------------------------------------------------------
# Valid entities — M2M (Set + Set on both sides)
# ---------------------------------------------------------------------------


class ValPaper(Entity):
    title: Req[str]
    tags: Set["ValPaperTag"]  # noqa: UP037


class ValPaperTag(Entity):
    name: Req[str]
    papers: Set[ValPaper]


# ---------------------------------------------------------------------------
# Invalid: Set[T] with no back-reference on T
# ---------------------------------------------------------------------------


class NoBackrefChild(Entity):
    text: Req[str]
    # No ManyToOne or Set pointing back to NoBackrefParent


class NoBackrefParent(Entity):
    title: Req[str]
    children: Set[NoBackrefChild]


# ---------------------------------------------------------------------------
# Invalid: Multiple ambiguous back-refs without reverse=
# ---------------------------------------------------------------------------


class AmbigOwner(Entity):
    title: Req[str]
    items: Set["AmbigItem"]  # noqa: UP037


class AmbigItem(Entity):
    label: Req[str]
    owner_a: Single[AmbigOwner]  # two back-refs → ambiguous
    owner_b: Single[AmbigOwner]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_validate_o2m_passes() -> None:
    """O2M with a proper ManyToOne back-ref should pass validation without error."""
    db = Database(entities=[ValBlog, ValBlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping()
    assert db.is_bound


def test_validate_m2m_passes() -> None:
    """M2M with Set on both sides should pass validation without error."""
    db = Database(entities=[ValPaper, ValPaperTag])
    db.bind("sqlite", ":memory:")
    db.generate_mapping()
    assert db.is_bound


def test_validate_missing_backref_raises_mapping_error() -> None:
    """Set[T] without a back-reference on T must raise MappingError."""
    db = Database(entities=[NoBackrefParent, NoBackrefChild])
    db.bind("sqlite", ":memory:")
    with pytest.raises(MappingError, match="back-reference"):
        db.generate_mapping()


def test_validate_ambiguous_backrefs_raises_mapping_error() -> None:
    """Multiple ManyToOne back-refs without reverse= must raise MappingError."""
    db = Database(entities=[AmbigOwner, AmbigItem])
    db.bind("sqlite", ":memory:")
    with pytest.raises(MappingError, match="ambiguous"):
        db.generate_mapping()


def test_validate_false_does_not_raise() -> None:
    """validate_relations=False (default) never raises even for broken schemas."""
    db = Database(entities=[NoBackrefParent, NoBackrefChild])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(validate_relations=False)  # must not raise


def test_mapping_error_is_exception() -> None:
    """MappingError can be raised and caught as a plain Exception."""
    with pytest.raises(Exception):  # noqa: B017
        raise MappingError("something went wrong")


def test_mapping_error_message_preserved() -> None:
    try:
        raise MappingError("sentinel message")
    except MappingError as exc:
        assert "sentinel message" in str(exc)


# ---------------------------------------------------------------------------
# New exception types
# ---------------------------------------------------------------------------


def test_object_not_found_is_exception() -> None:
    with pytest.raises(ObjectNotFound, match="row"):
        raise ObjectNotFound("row not found")


def test_multiple_objects_found_error_is_exception() -> None:
    with pytest.raises(MultipleObjectsFoundError, match="many"):
        raise MultipleObjectsFoundError("too many rows")


def test_constraint_error_is_exception() -> None:
    with pytest.raises(ConstraintError, match="unique"):
        raise ConstraintError("unique constraint failed")


def test_transaction_error_is_exception() -> None:
    with pytest.raises(TransactionError, match="deadlock"):
        raise TransactionError("deadlock detected")


def test_all_exceptions_subclass_exception() -> None:
    for cls in (
        ObjectNotFound,
        MultipleObjectsFoundError,
        ConstraintError,
        TransactionError,
    ):
        assert issubclass(cls, Exception)
