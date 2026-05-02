"""Tests for FieldSpec and RelationSpec option validation and normalization."""

import pytest

from nextorm.fields import FieldSpec


# --- FieldSpec tests ---
def test_fieldspec_columns_not_allowed() -> None:
    # columns is not a valid option for FieldSpec
    with pytest.raises(TypeError):
        FieldSpec(columns=["foo", "bar"])  # type: ignore


def test_fieldspec_all_options() -> None:
    # All valid FieldSpec options should be accepted
    spec = FieldSpec(
        primary_key=True,
        auto=True,
        nullable=True,
        unique=True,
        index=True,
        max_len=42,
        column="foo",
        sql_default="DEFAULT 1",
        sql_type="JSONB",
        volatile=True,
        uuid_auto="v7",
        precision=10,
        scale=2,
        unsigned=True,
        size=32,
        dimensions=384,
        lazy=True,
        autostrip=True,
        min=1,
        max=100,
    )
    assert spec.primary_key
    assert spec.auto
    assert spec.nullable
    assert spec.unique
    assert spec.index
    assert spec.max_len == 42
    assert spec.column == "foo"
    assert spec.sql_default == "DEFAULT 1"
    assert spec.sql_type == "JSONB"
    assert spec.volatile
    assert spec.uuid_auto == "v7"
    assert spec.precision == 10
    assert spec.scale == 2
    assert spec.unsigned
    assert spec.size == 32
    assert spec.dimensions == 384
    assert spec.lazy
    assert spec.autostrip
    assert spec.min == 1
    assert spec.max == 100


# --- RelationSpec/EntityMeta normalization tests ---
def test_relationspec_column_and_columns_exclusive() -> None:
    # Both column and columns set should raise only when normalized
    opts = dict(column="foo", columns=["foo"])
    from nextorm.entity import _normalize_singleopts_columns

    with pytest.raises(TypeError):
        _normalize_singleopts_columns(opts, "rel")


def test_relationspec_column_normalizes_columns() -> None:
    opts = dict(column="foo")
    from nextorm.entity import _normalize_singleopts_columns

    _normalize_singleopts_columns(opts, "rel")
    assert isinstance(opts["columns"], list)
    assert opts["columns"] == ["foo"]


def test_relationspec_columns_normalizes_column() -> None:
    opts = dict(columns=["bar"])
    from nextorm.entity import _normalize_singleopts_columns

    _normalize_singleopts_columns(opts, "rel")
    assert isinstance(opts["columns"], list)
    assert opts["columns"] == ["bar"]
    assert isinstance(opts["column"], str)
    assert opts["column"] == "bar"


def test_relationspec_columns_string_type() -> None:
    opts = dict(columns="foo")
    from nextorm.entity import _normalize_singleopts_columns

    _normalize_singleopts_columns(opts, "rel")
    assert isinstance(opts["columns"], list)
    assert opts["columns"] == ["foo"]
    assert isinstance(opts["column"], str)
    assert opts["column"] == "foo"


def test_relationspec_columns_invalid_type() -> None:
    opts = dict(columns=123)
    from nextorm.entity import _normalize_singleopts_columns

    with pytest.raises(TypeError):
        _normalize_singleopts_columns(opts, "rel")


def test_relationspec_columns_list_multiple() -> None:
    opts = dict(columns=["foo", "bar"])
    from nextorm.entity import _normalize_singleopts_columns

    _normalize_singleopts_columns(opts, "rel")
    assert isinstance(opts["columns"], list)
    assert opts["columns"] == ["foo", "bar"]
    assert opts.get("column") is None


def test_relationspec_column_and_columns_none() -> None:
    opts: dict[str, object] = dict()
    from nextorm.entity import _normalize_singleopts_columns

    _normalize_singleopts_columns(opts, "rel")
    assert opts.get("column") is None
    assert opts.get("columns") is None


def test_relationspec_reverse_columns_string_type() -> None:
    opts = dict(reverse_columns="foo")
    from nextorm.entity import _normalize_setopts_reverse_columns

    _normalize_setopts_reverse_columns(opts, "rel")
    assert isinstance(opts["reverse_columns"], list)
    assert opts["reverse_columns"] == ["foo"]
    assert isinstance(opts["reverse_column"], str)
    assert opts["reverse_column"] == "foo"


def test_relationspec_reverse_columns_invalid_type() -> None:
    opts = dict(reverse_columns=123)
    from nextorm.entity import _normalize_setopts_reverse_columns

    with pytest.raises(TypeError):
        _normalize_setopts_reverse_columns(opts, "rel")


def test_relationspec_reverse_columns_list_multiple() -> None:
    opts = dict(reverse_columns=["foo", "bar"])
    from nextorm.entity import _normalize_setopts_reverse_columns

    _normalize_setopts_reverse_columns(opts, "rel")
    assert isinstance(opts["reverse_columns"], list)
    assert opts["reverse_columns"] == ["foo", "bar"]
    assert opts.get("reverse_column") is None


# --- reverse_column (singular) normalization ---


def test_relationspec_reverse_column_alone_normalizes_to_list() -> None:
    """A single reverse_column string should also populate reverse_columns as a list."""
    opts = dict(reverse_column="owner_id")
    from nextorm.entity import _normalize_setopts_reverse_columns

    _normalize_setopts_reverse_columns(opts, "rel")
    assert opts["reverse_columns"] == ["owner_id"]  # type: ignore[comparison-overlap]
    assert opts["reverse_column"] == "owner_id"


def test_relationspec_reverse_column_non_string_raises() -> None:
    opts = {"reverse_column": 123}
    from nextorm.entity import _normalize_setopts_reverse_columns

    with pytest.raises(TypeError, match="must be a string"):
        _normalize_setopts_reverse_columns(opts, "rel")


def test_relationspec_reverse_columns_single_item_list_normalizes_reverse_column() -> None:
    """A single-item list for reverse_columns should also set reverse_column."""
    opts = dict(reverse_columns=["owner_id"])
    from nextorm.entity import _normalize_setopts_reverse_columns

    _normalize_setopts_reverse_columns(opts, "rel")
    assert opts["reverse_columns"] == ["owner_id"]
    assert opts["reverse_column"] == "owner_id"  # type: ignore[comparison-overlap]


def test_relationspec_reverse_column_and_reverse_columns_exclusive() -> None:
    opts = dict(reverse_column="a", reverse_columns=["a"])
    from nextorm.entity import _normalize_setopts_reverse_columns

    with pytest.raises(TypeError, match="cannot specify both"):
        _normalize_setopts_reverse_columns(opts, "rel")


# --- column (singular) non-string validation ---


def test_relationspec_column_non_string_raises() -> None:
    """A non-string column value should raise TypeError."""
    opts = {"column": 42}
    from nextorm.entity import _normalize_singleopts_columns

    with pytest.raises(TypeError, match="must be a string"):
        _normalize_singleopts_columns(opts, "rel")


# --- LocalSpec.has_default ---


def test_localspec_has_default_false_when_unset() -> None:
    from nextorm.fields import LocalSpec

    spec = LocalSpec()
    assert spec.has_default is False


def test_localspec_has_default_true_for_scalar() -> None:
    from nextorm.fields import LocalSpec

    spec = LocalSpec(default=0)
    assert spec.has_default is True


def test_localspec_has_default_true_for_callable() -> None:
    from nextorm.fields import LocalSpec

    spec = LocalSpec(default=list)
    assert spec.has_default is True
