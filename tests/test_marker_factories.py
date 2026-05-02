"""Covers Field/Relation marker factories, option normalization, and error branches."""

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

import pytest

from nextorm.entity import Entity
from nextorm.fields import (
    PK,
    DateTimeTz,
    Local,
    LongStr,
    Opt,
    Req,
    Set,
    Single,
    Vec,
    composite_index,
    composite_key,
    uuid7,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# --- Field marker factories ---
def test_req_factory_accepts_valid_options() -> None:
    marker = Req[int](index=True)
    assert isinstance(marker, Req)
    assert marker._options["index"] is True


def test_req_str_factory_accepts_max_len() -> None:
    marker = Req[str](max_len=10)
    assert isinstance(marker, Req)
    assert marker._options["max_len"] == 10


def test_req_factory_rejects_invalid_kwarg() -> None:
    with pytest.raises(TypeError):
        Req[int](foo=123)


def test_req_factory_too_many_args() -> None:
    with pytest.raises(TypeError):
        Req[str](1, 2)  # only one positional allowed for str (max_len)


def test_req_factory_duplicate_arg() -> None:
    with pytest.raises(TypeError):
        Req[str](1, max_len=2)  # duplicate for 'max_len'


def test_opt_factory_sets_nullable() -> None:
    marker = Opt[int]()
    # For int, nullable is not present in _options (handled by metaclass)
    assert isinstance(marker, Opt)


def test_pk_factory_sets_primary_key_and_auto() -> None:
    marker = PK[int]()
    # Only primary_key is present in _options
    assert marker._options["primary_key"] is True


# --- Relation marker factories ---
def test_single_factory_accepts_valid_options() -> None:
    marker = Single[Entity](reverse="parent")
    assert marker._options["reverse"] == "parent"


def test_single_factory_rejects_invalid_kwarg() -> None:
    with pytest.raises(TypeError):
        Single[Entity](foo=123)


def test_set_factory_accepts_valid_options() -> None:
    marker = Set[Entity](reverse="parent")
    assert marker._options["reverse"] == "parent"


def test_set_factory_rejects_invalid_kwarg() -> None:
    with pytest.raises(TypeError):
        Set[Entity](foo=123)


# --- Local marker ---
def test_local_marker_instantiation() -> None:
    marker = Local[int]()
    assert isinstance(marker, Local)


# --- Error: columns not allowed for FieldSpec ---
def test_req_factory_rejects_columns_option() -> None:
    with pytest.raises(TypeError):
        Req[int](columns=["foo"])


def test_opt_factory_rejects_columns_option() -> None:
    with pytest.raises(TypeError):
        Opt[int](columns=["foo"])


# --- Composite PK/Index constraints (basic smoke test) ---
def test_composite_key_and_index() -> None:
    ck = composite_key("a", "b")
    ci = composite_index("x", "y")
    assert ck.unique
    assert not ck.primary_key
    assert not ci.primary_key
    assert ck.fields == ("a", "b")
    assert ci.fields == ("x", "y")


# ---------------------------------------------------------------------------
# Excluded options — options that are not allowed for specific type/marker combos
# ---------------------------------------------------------------------------


def test_req_str_rejects_nullable() -> None:
    """Req[str] must not accept the nullable option (excluded for string fields)."""
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        Req[str](nullable=True)


def test_req_longstr_rejects_nullable() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        Req[LongStr](nullable=True)


def test_opt_int_rejects_auto() -> None:
    """Opt[int] must not accept auto (auto-increment is only for PK fields)."""
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        Opt[int](auto=True)


def test_opt_uuid7_rejects_uuid_auto() -> None:
    """Opt[uuid7] must not accept uuid_auto (excluded for Opt UUID fields)."""
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        Opt[uuid7](uuid_auto="v7")


def test_req_float_rejects_primary_key() -> None:
    """Req[float] must not accept primary_key (excluded for float fields)."""
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        Req[float](primary_key=True)


# ---------------------------------------------------------------------------
# DateTimeTz / timedelta / time type-specific options (DateTimeTypeOpts path)
# ---------------------------------------------------------------------------


def test_req_datetime_factory_instantiates() -> None:
    marker = Req[datetime]()
    assert isinstance(marker, Req)


def test_req_datetimetz_factory_instantiates() -> None:
    marker = Req[DateTimeTz]()
    assert isinstance(marker, Req)


def test_req_timedelta_factory_instantiates() -> None:
    marker = Req[timedelta]()
    assert isinstance(marker, Req)


def test_req_time_factory_instantiates() -> None:
    marker = Req[time]()
    assert isinstance(marker, Req)


# ---------------------------------------------------------------------------
# Vec type-specific options (VecTypeOpts path)
# ---------------------------------------------------------------------------


def test_req_vec_factory_accepts_dimensions() -> None:
    marker = Req[Vec](dimensions=128)
    assert isinstance(marker, Req)
    assert marker._options["dimensions"] == 128


# ---------------------------------------------------------------------------
# UUID positional argument shorthand
# ---------------------------------------------------------------------------


def test_req_uuid7_positional_uuid_auto() -> None:
    """Req[uuid7] accepts uuid_auto as the first positional argument."""
    marker = Req[uuid7]("v7")
    assert marker._options["uuid_auto"] == "v7"


def test_req_uuid7_duplicate_positional_and_kwarg_raises() -> None:
    with pytest.raises(TypeError, match="multiple values"):
        Req[uuid7]("v7", uuid_auto="v7")


# ---------------------------------------------------------------------------
# PK[Entity] — relation-style primary key
# ---------------------------------------------------------------------------


def test_pk_entity_type_creates_relation_marker() -> None:
    """PK[SomeEntity] must produce a relation marker (not a field marker)."""
    from nextorm.fields import Single  # noqa: PLC0415

    marker_cls = PK[Entity]
    # It should be a subclass of Single (relation-style PK)
    assert issubclass(marker_cls, Single)


def test_pk_entity_relation_marker_sets_primary_key() -> None:
    """Instantiating PK[Entity]() should automatically set primary_key=True."""
    marker = PK[Entity]()
    assert marker._options.get("primary_key") is True


# ---------------------------------------------------------------------------
# Local marker — default and py_check options
# ---------------------------------------------------------------------------


def test_local_marker_with_default_scalar() -> None:
    marker = Local[int](default=0)
    assert marker._options["default"] == 0


def test_local_marker_with_default_callable() -> None:
    marker = Local[list](default=list)  # type: ignore[type-arg]
    assert marker._options["default"] is list


def test_local_marker_with_py_check() -> None:
    check: Callable[[object], bool] = lambda v: isinstance(v, int) and v > 0  # noqa: E731
    marker = Local[int](py_check=check)
    assert marker._options["py_check"] is check


def test_local_marker_rejects_unknown_kwarg() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        Local[int](foo=1)


# --- Opt[str]() marker call → triggers the nullable default path (line 261 in fields.py) ---


def test_opt_str_marker_empty_call_sets_nullable() -> None:
    """Opt[str]() called with no args should set nullable=True via the PEP path."""
    marker = Opt[str]()
    assert marker._options.get("nullable") is True


# ---------------------------------------------------------------------------
# Marker stubs — __get__ and __set__ raise NotImplementedError
# These stubs exist purely for the type checker; EntityMeta replaces them
# with actual descriptors at class-definition time.
# ---------------------------------------------------------------------------


def test_pk_get_raises_not_implemented() -> None:
    marker = PK[int]()
    with pytest.raises(NotImplementedError):
        marker.__get__(object(), type)


def test_pk_set_raises_not_implemented() -> None:
    marker = PK[int]()
    with pytest.raises(NotImplementedError):
        marker.__set__(object(), 1)


def test_field_get_raises_not_implemented() -> None:
    marker = Req[int]()
    with pytest.raises(NotImplementedError):
        marker.__get__(object(), type)


def test_field_set_raises_not_implemented() -> None:
    marker = Req[str]()
    with pytest.raises(NotImplementedError):
        marker.__set__(object(), "x")


def test_single_get_raises_not_implemented() -> None:
    marker = Single[Entity]()
    with pytest.raises(NotImplementedError):
        marker.__get__(object(), type)


def test_single_set_raises_not_implemented() -> None:
    marker = Single[Entity]()
    with pytest.raises(NotImplementedError):
        marker.__set__(object(), None)  # type: ignore[arg-type]


def test_set_get_raises_not_implemented() -> None:
    marker = Set[Entity]()
    with pytest.raises(NotImplementedError):
        marker.__get__(object(), type)


def test_set_set_raises_not_implemented() -> None:
    marker = Set[Entity]()
    with pytest.raises(NotImplementedError):
        marker.__set__(object(), [])


def test_local_get_raises_not_implemented() -> None:
    marker = Local[int]()
    with pytest.raises(NotImplementedError):
        marker.__get__(object(), type)


def test_local_set_raises_not_implemented() -> None:
    marker = Local[int]()
    with pytest.raises(NotImplementedError):
        marker.__set__(object(), 1)
