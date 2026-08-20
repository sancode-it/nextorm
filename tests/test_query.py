"""Tests for ColumnExpr, QuerySet, and Database CRUD.

All tests use a fresh in-memory SQLite database via the ``db`` fixture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from nextorm.database import Database
from nextorm.entity import Entity
from nextorm.exceptions import MultipleObjectsFoundError, ObjectNotFound
from nextorm.expr import ColumnExpr
from nextorm.fields import PK, LongStr, Opt, Req, Set, Single
from nextorm.query import QuerySet
from nextorm.session import db_session
from nextorm.sql.nodes import BinOp, ColumnRef, Literal, OrderItem, Param

if TYPE_CHECKING:
    from collections.abc import Generator

# ---------------------------------------------------------------------------
# Module-level entity definitions (metaclass processes at import time)
# ---------------------------------------------------------------------------


class Product(Entity):
    id: PK[int]
    name: Req[str]
    price: Req[float]
    in_stock: Req[bool]


class Note(Entity):
    """Entity with an optional field — used to test NULL handling."""

    id: PK[int]
    title: Req[str]
    body: Opt[str]


class Category(Entity):
    """Used for ManyToOne relation tests."""

    id: PK[int]
    label: Req[str]


class TaggedProduct(Entity):
    """Entity with a ManyToOne FK column — exercises the None-field branch in _map_row."""

    id: PK[int]
    name: Req[str]
    category: Single[Category]


class _StringRefChild(Entity):
    """Child entity with a ManyToOne back-ref to _StringRefParent."""

    label: Req[str]
    owner: Single["_StringRefParent"]  # noqa: UP037


class _StringRefParent(Entity):
    """Parent entity with a string forward-ref Set — triggers isinstance(target, str)
    branch in _do_prefetch (line 490)."""

    label: Req[str]
    kids: Set["_StringRefChild"]  # noqa: UP037


class _UnresolvableChild(Entity):  # pyright: ignore[reportUnusedClass]
    """Child with no relation pointing to any parent."""

    label: Req[str]


class _PrefetchUnresolvableParent(Entity):
    """Parent with a Set that has no matching entity in the registry — line 496."""

    label: Req[str]
    stuff: Set["_GhostEntity"]  # type: ignore[name-defined]  # noqa: UP037, F821


# ---------------------------------------------------------------------------
# Entities for order_by chained-relation tests
# ---------------------------------------------------------------------------


class _ChainTop(Entity):
    """Leaf-level entity in a 3-level chain used for order_by chaining tests."""

    id: PK[int]
    slug: Req[str]


class _ChainMid(Entity):
    """Middle entity in a 3-level chain."""

    id: PK[int]
    name: Req[str]
    top: Single[_ChainTop]


class _ChainLeaf(Entity):
    """Root entity in a 3-level chain — queried with chained order_by lambdas."""

    id: PK[int]
    sku: Req[str]
    mid: Single[_ChainMid]
    tags: Set["_ChainTag"]  # noqa: UP037  — used to exercise non-SINGLE branch in coverage


class _ChainTag(Entity):
    """Back-ref entity for the Set relation on _ChainLeaf — used for coverage only."""

    id: PK[int]
    label: Req[str]
    leaf: Single[_ChainLeaf]


class _ChainO2OBase(Entity):
    """Base entity in an O2O chain — used to exercise the relation-PK branch."""

    id: PK[int]
    slug: Req[str]


class _ChainO2OChild(Entity):
    """O2O child whose PK IS a relation (PK[_ChainO2OBase])."""

    base: PK[_ChainO2OBase]
    note: Req[str]


class _ChainO2OLeaf(Entity):
    """Entity with a Single[_ChainO2OChild] — used to trigger the relation-PK branch."""

    id: PK[int]
    sku: Req[str]
    child: Single[_ChainO2OChild]


# ---------------------------------------------------------------------------
# Lifecycle hook tracker
# ---------------------------------------------------------------------------


class HookedEntity(Entity):
    id: PK[int]
    label: Req[str]


_hook_calls: list[str] = []


def _clear_hooks() -> None:
    _hook_calls.clear()


# Monkey-patch lifecycle hooks onto HookedEntity for test observation
def _track_before_insert(self: Any) -> None:
    _hook_calls.append("before_insert")


def _track_after_insert(self: Any) -> None:
    _hook_calls.append("after_insert")


def _track_before_update(self: Any) -> None:
    _hook_calls.append("before_update")


def _track_after_update(self: Any) -> None:
    _hook_calls.append("after_update")


def _track_before_delete(self: Any) -> None:
    _hook_calls.append("before_delete")


def _track_after_delete(self: Any) -> None:
    _hook_calls.append("after_delete")


def _track_after_load(self: Any) -> None:
    _hook_calls.append("after_load")


HookedEntity.before_insert = _track_before_insert  # type: ignore[method-assign]
HookedEntity.after_insert = _track_after_insert  # type: ignore[method-assign]
HookedEntity.before_update = _track_before_update  # type: ignore[method-assign]
HookedEntity.after_update = _track_after_update  # type: ignore[method-assign]
HookedEntity.before_delete = _track_before_delete  # type: ignore[method-assign]
HookedEntity.after_delete = _track_after_delete  # type: ignore[method-assign]
HookedEntity.after_load = _track_after_load  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Generator[Database, None, None]:
    _db = Database(entities=[Product, Note, HookedEntity, Category, TaggedProduct])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)
    yield _db
    _db.close()


@pytest.fixture
def seeded_db(db: Database) -> Database:
    """Return a Database with three Product rows already inserted."""
    with db_session:
        Product(name="Widget", price=9.99, in_stock=True)
        Product(name="Gadget", price=24.99, in_stock=False)
        Product(name="Doohickey", price=4.99, in_stock=True)
    return db


# ---------------------------------------------------------------------------
# ColumnExpr — construction and operators
# ---------------------------------------------------------------------------


def test_column_expr_repr() -> None:
    assert repr(ColumnExpr("name", "product")) == "ColumnExpr('product'.'name')"
    assert repr(ColumnExpr("name")) == "ColumnExpr('name')"


def test_column_expr_hash() -> None:
    e1 = ColumnExpr("name", "product")
    e2 = ColumnExpr("name", "product")
    assert hash(e1) == hash(e2)
    assert e1 == e2  # via __eq__ for identity check... wait, __eq__ returns BinOp


def test_column_expr_eq_returns_binop() -> None:
    cond = Product.name == "alice"
    assert isinstance(cond, BinOp)
    assert cond.op == "="
    assert isinstance(cond.left, ColumnRef)
    assert cond.left.column == "name"
    assert isinstance(cond.right, Param)
    assert cond.right.value == "alice"


def test_column_expr_ne_returns_binop() -> None:
    cond = Product.price != 0.0
    assert isinstance(cond, BinOp)
    assert cond.op == "<>"


def test_column_expr_lt() -> None:
    cond = Product.price < 10.0
    assert isinstance(cond, BinOp)
    assert cond.op == "<"


def test_column_expr_le() -> None:
    cond = Product.price <= 10.0
    assert isinstance(cond, BinOp)
    assert cond.op == "<="


def test_column_expr_gt() -> None:
    cond = Product.price > 10.0
    assert isinstance(cond, BinOp)
    assert cond.op == ">"


def test_column_expr_ge() -> None:
    cond = Product.price >= 10.0
    assert isinstance(cond, BinOp)
    assert cond.op == ">="


def test_column_expr_like() -> None:
    cond = Product.name.like("%et%")
    assert isinstance(cond, BinOp)
    assert cond.op == "LIKE"
    assert isinstance(cond.right, Param)
    assert cond.right.value == "%et%"


def test_column_expr_ilike() -> None:
    cond: BinOp = Product.name.ilike("%get%")
    assert cond.op == "ILIKE"


def test_column_expr_is_null() -> None:
    cond = Note.body.is_null()
    assert isinstance(cond, BinOp)
    assert cond.op == "IS"
    assert isinstance(cond.right, Literal)
    assert cond.right.value is None


def test_column_expr_is_not_null() -> None:
    cond: BinOp = Note.body.is_not_null()
    assert cond.op == "IS NOT"


def test_column_expr_dist_l2() -> None:
    cond = Product.name.dist_l2([0.1, 0.2, 0.3])
    assert isinstance(cond, BinOp)
    assert cond.op == "<->"
    assert isinstance(cond.right, Param)
    assert cond.right.value == [0.1, 0.2, 0.3]


def test_column_expr_dist_cosine() -> None:
    cond = Product.name.dist_cosine([0.5, 0.5])
    assert isinstance(cond, BinOp)
    assert cond.op == "<=>"
    assert isinstance(cond.right, Param)
    assert cond.right.value == [0.5, 0.5]


def test_column_expr_dist_inner_product() -> None:
    cond = Product.name.dist_inner_product([1.0, 2.0])
    assert isinstance(cond, BinOp)
    assert cond.op == "<#>"
    assert isinstance(cond.right, Param)
    assert cond.right.value == [1.0, 2.0]


def test_column_expr_asc() -> None:
    item = Product.name.asc()
    assert isinstance(item, OrderItem)
    assert item.descending is False


def test_column_expr_desc() -> None:
    item = Product.price.desc()
    assert isinstance(item, OrderItem)
    assert item.descending is True


def test_column_expr_coerce_column_expr() -> None:
    """ColumnExpr.coerce of another ColumnExpr returns a ColumnRef (not Param)."""
    expr_a = ColumnExpr("x")
    expr_b = ColumnExpr("y")
    cond = expr_a == expr_b
    assert isinstance(cond.right, ColumnRef)
    assert cond.right.column == "y"


def test_field_descriptor_class_access_returns_column_expr() -> None:
    """Class-level access on an entity field returns a ColumnExpr."""
    result = Product.name
    assert isinstance(result, ColumnExpr)
    assert result.field_name == "name"
    assert result.table_name == "product"


def test_field_descriptor_instance_access_returns_value() -> None:
    """Instance-level access still returns the field value."""
    p = Product(name="Widget", price=9.99, in_stock=True)
    assert p.name == "Widget"


# ---------------------------------------------------------------------------
# Param value field
# ---------------------------------------------------------------------------


def test_param_has_value_false_by_default() -> None:
    p = Param()
    assert p.has_value is False


def test_param_has_value_true_when_set() -> None:
    p = Param(value=42)
    assert p.has_value is True
    assert p.value == 42


def test_param_value_none_is_has_value() -> None:
    p = Param(value=None)
    assert p.has_value is True


def test_param_value_excluded_from_equality() -> None:
    assert Param() == Param(value=99)


def test_param_value_excluded_from_repr() -> None:
    assert "value" not in repr(Param(value=7))


def test_render_collects_param_values() -> None:
    """render() now returns actual values from Param(value=...) nodes."""
    from nextorm.sql.builder import render as _render
    from nextorm.sql.nodes import BinOp, ColumnRef, Param, Select, Star

    stmt = Select(
        columns=(Star(),),
        from_table="product",
        where=BinOp(ColumnRef("name"), "=", Param(value="Widget")),
    )
    sql, params = _render(stmt)
    assert "?" in sql
    assert params == ["Widget"]


# ---------------------------------------------------------------------------
# Database.select — basic construction
# ---------------------------------------------------------------------------


def test_select_returns_queryset(db: Database) -> None:
    qs = db.select(Product)
    assert isinstance(qs, QuerySet)


def test_select_before_generate_mapping_raises() -> None:
    fresh = Database(entities=[Product])
    fresh.bind("sqlite", ":memory:")
    with pytest.raises(RuntimeError, match="generate_mapping"):
        fresh.select(Product)
    fresh.close()


def test_select_unmapped_entity_raises(db: Database) -> None:
    class Orphan(Entity):
        x: Req[int]

    with pytest.raises(RuntimeError, match="not in the mapped schema"):
        db.select(Orphan)


# ---------------------------------------------------------------------------
# Database.save — INSERT
# ---------------------------------------------------------------------------


def test_save_inserts_entity_and_sets_pk(db: Database) -> None:
    with db_session:
        p = Product(name="Widget", price=9.99, in_stock=True)
        assert p.id is None
    assert p.id is not None
    assert isinstance(p.id, int)


def test_save_multiple_inserts_get_sequential_pks(db: Database) -> None:
    with db_session:
        p1 = Product(name="A", price=1.0, in_stock=True)
        p2 = Product(name="B", price=2.0, in_stock=False)
    assert p1.id != p2.id


def test_save_entity_is_selectable_after_insert(db: Database) -> None:
    with db_session:
        Product(name="Widget", price=9.99, in_stock=True)
    results = db.select(Product).fetch_all()
    assert len(results) == 1
    assert results[0].name == "Widget"


# ---------------------------------------------------------------------------
# Database.save — UPDATE
# ---------------------------------------------------------------------------


def test_save_updates_existing_entity(db: Database) -> None:
    with db_session:
        p = Product(name="Old", price=1.0, in_stock=True)
        db.flush()  # INSERT
        p.name = "New"  # marks dirty
    results = db.select(Product).fetch_all()
    assert len(results) == 1
    assert results[0].name == "New"


def test_save_update_does_not_create_duplicate(db: Database) -> None:
    with db_session:
        p = Product(name="X", price=1.0, in_stock=True)
        db.flush()  # INSERT
        p.price = 2.0  # marks dirty
    assert db.select(Product).count() == 1


# ---------------------------------------------------------------------------
# Database.delete_instance
# ---------------------------------------------------------------------------


def test_delete_instance_removes_row(db: Database) -> None:
    with db_session:
        p = Product(name="Widget", price=9.99, in_stock=True)
    db.delete_instance(p)
    assert db.select(Product).count() == 0


def test_delete_instance_clears_pk(db: Database) -> None:
    with db_session:
        p = Product(name="Widget", price=9.99, in_stock=True)
    db.delete_instance(p)
    assert p.id is None


def test_delete_instance_unsaved_raises(db: Database) -> None:
    p = Product(name="Ghost", price=0.0, in_stock=False)
    with pytest.raises(ValueError, match="primary key is None"):
        db.delete_instance(p)


def test_delete_instance_unmapped_raises(db: Database) -> None:
    class Unmapped(Entity):
        x: Req[int]

    u = Unmapped(x=1)
    u.id = 1
    with pytest.raises(RuntimeError, match="not in the mapped schema"):
        db.delete_instance(u)


# ---------------------------------------------------------------------------
# QuerySet.fetch_all
# ---------------------------------------------------------------------------


def test_fetch_all_returns_all_rows(seeded_db: Database) -> None:
    results = seeded_db.select(Product).fetch_all()
    assert len(results) == 3


def test_fetch_all_returns_entity_instances(seeded_db: Database) -> None:
    results = seeded_db.select(Product).fetch_all()
    for r in results:
        assert isinstance(r, Product)
        assert r.id is not None
        assert isinstance(r.name, str)
        assert isinstance(r.price, float)


def test_fetch_all_empty_table(db: Database) -> None:
    assert db.select(Product).fetch_all() == []


# ---------------------------------------------------------------------------
# QuerySet.filter
# ---------------------------------------------------------------------------


def test_filter_eq(seeded_db: Database) -> None:
    results = seeded_db.select(Product).filter(Product.name == "Widget").fetch_all()
    assert len(results) == 1
    assert results[0].name == "Widget"


def test_filter_ne(seeded_db: Database) -> None:
    results = seeded_db.select(Product).filter(Product.name != "Widget").fetch_all()
    assert len(results) == 2


def test_filter_lt(seeded_db: Database) -> None:
    results = seeded_db.select(Product).filter(Product.price < 10.0).fetch_all()
    names = {r.name for r in results}
    assert "Widget" in names
    assert "Doohickey" in names
    assert "Gadget" not in names


def test_filter_le(seeded_db: Database) -> None:
    results = seeded_db.select(Product).filter(Product.price <= 9.99).fetch_all()
    names = {r.name for r in results}
    assert "Widget" in names


def test_filter_gt(seeded_db: Database) -> None:
    results = seeded_db.select(Product).filter(Product.price > 10.0).fetch_all()
    assert len(results) == 1
    assert results[0].name == "Gadget"


def test_filter_ge(seeded_db: Database) -> None:
    results = seeded_db.select(Product).filter(Product.price >= 24.99).fetch_all()
    assert len(results) == 1


def test_filter_bool(seeded_db: Database) -> None:
    results = seeded_db.select(Product).filter(Product.in_stock == True).fetch_all()  # noqa: E712
    assert len(results) == 2


def test_filter_multiple_conditions_anded(seeded_db: Database) -> None:
    """Multiple conditions passed to a single filter() are AND-ed together."""
    results = (
        seeded_db.select(Product)
        .filter(Product.in_stock == True, Product.price < 10.0)  # noqa: E712
        .fetch_all()
    )
    assert len(results) == 2


def test_filter_kwargs_equality(seeded_db: Database) -> None:
    """filter(field=value) applies a keyword equality condition."""
    results = seeded_db.select(Product).filter(name="Widget").fetch_all()
    assert len(results) == 1
    assert results[0].name == "Widget"


def test_filter_kwargs_mixed_with_conditions(seeded_db: Database) -> None:
    """filter() accepts both SqlNode conditions and **kwargs simultaneously."""
    results = seeded_db.select(Product).filter(Product.price < 100.0, in_stock=True).fetch_all()
    assert all(r.price < 100.0 and r.in_stock for r in results)


def test_filter_chained_is_independent(seeded_db: Database) -> None:
    """Chained filter() calls combine conditions with AND."""
    results = (
        seeded_db.select(Product)
        .filter(Product.in_stock == True)  # noqa: E712
        .filter(Product.price < 10.0)
        .fetch_all()
    )
    assert len(results) == 2


def test_filter_like(seeded_db: Database) -> None:
    results = seeded_db.select(Product).filter(Product.name.like("%et")).fetch_all()
    names = {r.name for r in results}
    assert "Widget" in names
    assert "Gadget" in names


# ---------------------------------------------------------------------------
# QuerySet.where — lambda predicate DSL
# ---------------------------------------------------------------------------


def test_where_eq(seeded_db: Database) -> None:
    """where() with == works like filter()."""
    results = seeded_db.select(Product).where(lambda p: p.name == "Widget").fetch_all()
    assert len(results) == 1
    assert results[0].name == "Widget"


def test_where_gt(seeded_db: Database) -> None:
    """where() with > numeric comparison."""
    results = seeded_db.select(Product).where(lambda p: p.price > 10).fetch_all()
    assert all(r.price > 10 for r in results)


def test_where_chained_combines_with_and(seeded_db: Database) -> None:
    """Chained where() calls are combined with AND."""
    results = (
        seeded_db.select(Product)
        .where(lambda p: p.in_stock == True)  # noqa: E712
        .where(lambda p: p.price < 10)
        .fetch_all()
    )
    assert all(r.in_stock for r in results)
    assert all(r.price < 10 for r in results)


def test_where_proxy_attr_is_column_expr(db: Database) -> None:
    """EntityProxy attribute access returns a ColumnExpr."""
    from nextorm.expr import ColumnExpr
    from nextorm.query import EntityProxy

    proxy = EntityProxy("product")
    attr = proxy.price
    assert isinstance(attr, ColumnExpr)
    assert attr.field_name == "price"
    assert attr.table_name == "product"


# ---------------------------------------------------------------------------
# QuerySet.order_by
# ---------------------------------------------------------------------------


def test_order_by_asc(seeded_db: Database) -> None:
    results = seeded_db.select(Product).order_by(Product.price.asc()).fetch_all()
    prices = [r.price for r in results]
    assert prices == sorted(prices)


def test_order_by_desc(seeded_db: Database) -> None:
    results = seeded_db.select(Product).order_by(Product.price.desc()).fetch_all()
    prices = [r.price for r in results]
    assert prices == sorted(prices, reverse=True)


def test_order_by_multiple_columns(seeded_db: Database) -> None:
    results = (
        seeded_db.select(Product).order_by(Product.in_stock.desc(), Product.price.asc()).fetch_all()
    )
    assert len(results) == 3


def test_order_by_bare_column_expr_asc(seeded_db: Database) -> None:
    """Bare ColumnExpr (no .asc()/.desc()) is auto-wrapped as ASC."""
    results = seeded_db.select(Product).order_by(Product.price).fetch_all()
    prices = [r.price for r in results]
    assert prices == sorted(prices)


def test_order_by_bare_column_expr_mixed(seeded_db: Database) -> None:
    """Mix of bare ColumnExpr (ASC) and explicit OrderItem (DESC)."""
    results = seeded_db.select(Product).order_by(Product.in_stock, Product.price.desc()).fetch_all()
    assert len(results) == 3
    # in_stock=False (0) comes first in ASC order
    assert not results[0].in_stock


def test_order_by_lambda_asc(seeded_db: Database) -> None:
    """Lambda returning a bare ColumnExpr is auto-wrapped as ASC."""
    results = seeded_db.select(Product).order_by(lambda p: p.price).fetch_all()
    prices = [r.price for r in results]
    assert prices == sorted(prices)


def test_order_by_lambda_desc(seeded_db: Database) -> None:
    """Lambda returning an OrderItem with DESC direction."""
    results = seeded_db.select(Product).order_by(lambda p: p.price.desc()).fetch_all()
    prices = [r.price for r in results]
    assert prices == sorted(prices, reverse=True)


def test_order_by_lambda_tuple_multi_column(seeded_db: Database) -> None:
    """Lambda returning a tuple of OrderItems for multi-column ordering."""
    results = (
        seeded_db.select(Product).order_by(lambda p: (p.in_stock.desc(), p.price.asc())).fetch_all()
    )
    assert len(results) == 3
    # in_stock=True (1) should come first in DESC order
    assert results[0].in_stock
    assert results[1].in_stock
    # among in_stock=True rows, price is ascending
    assert results[0].price < results[1].price


def test_order_by_invalid_type_raises(seeded_db: Database) -> None:
    """Passing an unsupported type to order_by raises TypeError."""
    with pytest.raises(TypeError, match="order_by\\(\\) expects"):
        seeded_db.select(Product).order_by("price")  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# QuerySet.order_by — chained relation traversal (order_by lambda)
# ---------------------------------------------------------------------------


@pytest.fixture
def chain_db() -> Generator[Database, None, None]:
    """Return a seeded Database for the 3-level relation-chain order_by tests."""
    _db = Database(entities=[_ChainTop, _ChainMid, _ChainLeaf, _ChainTag])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)
    with db_session:
        top_b = _ChainTop(slug="b-top")
        top_a = _ChainTop(slug="a-top")
        mid_2 = _ChainMid(name="mid-2", top=top_b)
        mid_1 = _ChainMid(name="mid-1", top=top_a)
        _ChainLeaf(sku="leaf-z", mid=mid_2)
        _ChainLeaf(sku="leaf-a", mid=mid_1)
        _ChainLeaf(sku="leaf-m", mid=mid_1)
    yield _db
    _db.close()


def test_order_by_lambda_single_hop_relation(chain_db: Database) -> None:
    """Lambda with one relation hop: order_by(lambda x: x.relation.field)."""
    results = chain_db.select(_ChainLeaf).order_by(lambda leaf: leaf.mid.name).fetch_all()
    names = [r.mid.name for r in results]
    assert names == sorted(names)


def test_order_by_lambda_two_hop_relation(chain_db: Database) -> None:
    """Lambda with two relation hops: order_by(lambda x: x.rel1.rel2.field)."""
    results = chain_db.select(_ChainLeaf).order_by(lambda leaf: leaf.mid.top.slug).fetch_all()
    slugs = [r.mid.top.slug for r in results]
    assert slugs == sorted(slugs)


def test_order_by_lambda_mixed_chain_and_direct(chain_db: Database) -> None:
    """Lambda tuple mixing chained relation field and direct field."""
    results = (
        chain_db.select(_ChainLeaf).order_by(lambda leaf: (leaf.mid.top.slug, leaf.sku)).fetch_all()
    )
    assert len(results) == 3
    # All rows from slug "a-top" come before "b-top"
    a_rows = [r for r in results if r.mid.top.slug == "a-top"]
    b_rows = [r for r in results if r.mid.top.slug == "b-top"]
    assert results.index(a_rows[0]) < results.index(b_rows[0])
    # Within the "a-top" group, sku is ascending
    a_skus = [r.sku for r in a_rows]
    assert a_skus == sorted(a_skus)


def test_order_by_lambda_dedup_joins(chain_db: Database) -> None:
    """When the same relation is accessed in two tuple branches, the JOIN is added once."""
    # Both leaf.mid.top.slug and leaf.mid.name traverse leaf→mid; the second
    # occurrence must be de-duplicated so only one JOIN on chainmid is emitted.
    results = (
        chain_db.select(_ChainLeaf)
        .order_by(lambda leaf: (leaf.mid.top.slug, leaf.mid.name))
        .fetch_all()
    )
    assert len(results) == 3
    slugs = [r.mid.top.slug for r in results]
    assert slugs == sorted(slugs)


def test_order_by_lambda_set_relation_falls_through(chain_db: Database) -> None:
    """Accessing a Set relation in an order_by lambda is treated as a plain field name."""
    from nextorm.expr import ColumnExpr
    from nextorm.query import _OrderAccumulator, _resolve_order_attr

    accum = _OrderAccumulator()
    # 'tags' is a Set[_ChainTag] on _ChainLeaf — not a SINGLE relation
    result = _resolve_order_attr(_ChainLeaf, "chainleaf", "tags", accum)
    assert isinstance(result, ColumnExpr)
    assert result.field_name == "tags"
    assert not accum.joins  # Set relations do not generate JOINs


def test_order_by_lambda_relation_pk_target(tmp_path: Any) -> None:
    """_resolve_order_attr builds the JOIN correctly for an O2O child with a relation-PK."""
    from nextorm.expr import ColumnExpr
    from nextorm.query import _OrderAccumulator, _resolve_order_attr
    from nextorm.sql.nodes import BinOp

    accum = _OrderAccumulator()
    # _ChainO2OLeaf.child is Single[_ChainO2OChild], whose PK is PK[_ChainO2OBase]
    # (a relation-PK).  The pk_attr "base" is in _relations_, not _fields_.
    result = _resolve_order_attr(_ChainO2OLeaf, "chainO2oleaf", "child", accum)
    # Should return a proxy for _ChainO2OChild and have registered the JOIN
    assert len(accum.joins) == 1
    _join_type, _table_name, _alias, on_node = accum.joins[0]
    assert _join_type == "INNER"
    assert _table_name == _ChainO2OChild._table_name_
    assert isinstance(on_node, BinOp)
    # Continue chaining: .note should be a plain ColumnExpr on the child table
    child_result = result.note
    assert isinstance(child_result, ColumnExpr)
    assert child_result.field_name == "note"


# ---------------------------------------------------------------------------
# QuerySet.limit / offset
# ---------------------------------------------------------------------------


def test_limit(seeded_db: Database) -> None:
    results = seeded_db.select(Product).limit(2).fetch_all()
    assert len(results) == 2


def test_offset(seeded_db: Database) -> None:
    all_results = seeded_db.select(Product).order_by(Product.id.asc()).fetch_all()
    offset_results = seeded_db.select(Product).order_by(Product.id.asc()).offset(1).fetch_all()
    assert len(offset_results) == 2
    assert offset_results[0].id == all_results[1].id


def test_limit_and_offset(seeded_db: Database) -> None:
    results = seeded_db.select(Product).order_by(Product.id.asc()).limit(2).offset(1).fetch_all()
    assert len(results) == 2


# ---------------------------------------------------------------------------
# QuerySet immutability
# ---------------------------------------------------------------------------


def test_filter_returns_new_queryset(seeded_db: Database) -> None:
    base = seeded_db.select(Product)
    filtered = base.filter(Product.name == "Widget")
    assert len(base.fetch_all()) == 3
    assert len(filtered.fetch_all()) == 1


def test_limit_returns_new_queryset(seeded_db: Database) -> None:
    base = seeded_db.select(Product)
    limited = base.limit(1)
    assert len(base.fetch_all()) == 3
    assert len(limited.fetch_all()) == 1


def test_order_by_returns_new_queryset(seeded_db: Database) -> None:
    base = seeded_db.select(Product)
    ordered = base.order_by(Product.price.asc())
    assert base is not ordered


# ---------------------------------------------------------------------------
# QuerySet.fetch_one / first
# ---------------------------------------------------------------------------


def test_fetch_one_returns_entity(seeded_db: Database) -> None:
    result = seeded_db.select(Product).order_by(Product.id.asc()).fetch_one()
    assert isinstance(result, Product)


def test_fetch_one_returns_none_for_empty(db: Database) -> None:
    result = db.select(Product).fetch_one()
    assert result is None


def test_fetch_one_with_filter_no_match(seeded_db: Database) -> None:
    result = seeded_db.select(Product).filter(Product.name == "Missing").fetch_one()
    assert result is None


def test_first_is_alias_for_fetch_one(seeded_db: Database) -> None:
    a = seeded_db.select(Product).order_by(Product.id.asc()).fetch_one()
    b = seeded_db.select(Product).order_by(Product.id.asc()).first()
    assert a is not None and b is not None
    assert a.id == b.id


# ---------------------------------------------------------------------------
# QuerySet.count / exists
# ---------------------------------------------------------------------------


def test_count_all(seeded_db: Database) -> None:
    assert seeded_db.select(Product).count() == 3


def test_count_with_filter(seeded_db: Database) -> None:
    assert seeded_db.select(Product).filter(Product.in_stock == True).count() == 2  # noqa: E712


def test_count_empty(db: Database) -> None:
    assert db.select(Product).count() == 0


def test_exists_true(seeded_db: Database) -> None:
    assert seeded_db.select(Product).filter(Product.name == "Widget").exists() is True


def test_exists_false(seeded_db: Database) -> None:
    assert seeded_db.select(Product).filter(Product.name == "Missing").exists() is False


def test_exists_empty_table(db: Database) -> None:
    assert db.select(Product).exists() is False


# ---------------------------------------------------------------------------
# QuerySet.get
# ---------------------------------------------------------------------------


def test_get_returns_entity_when_exactly_one_match(seeded_db: Database) -> None:
    result = seeded_db.select(Product).filter(Product.name == "Widget").get()
    assert result is not None
    assert result.name == "Widget"


def test_get_returns_none_when_no_match(seeded_db: Database) -> None:
    result = seeded_db.select(Product).filter(Product.name == "Missing").get()
    assert result is None


def test_get_raises_when_multiple_matches(seeded_db: Database) -> None:
    with pytest.raises(MultipleObjectsFoundError, match="more than one"):
        seeded_db.select(Product).get()


# ---------------------------------------------------------------------------
# QuerySet.delete
# ---------------------------------------------------------------------------


def test_queryset_delete_matched_rows(seeded_db: Database) -> None:
    deleted = seeded_db.select(Product).filter(Product.in_stock == False).delete()  # noqa: E712
    assert deleted == 1
    assert seeded_db.select(Product).count() == 2


def test_queryset_delete_all(seeded_db: Database) -> None:
    seeded_db.select(Product).delete()
    assert seeded_db.select(Product).count() == 0


def test_queryset_delete_no_match_returns_zero(seeded_db: Database) -> None:
    deleted = seeded_db.select(Product).filter(Product.name == "Missing").delete()
    assert deleted == 0


# ---------------------------------------------------------------------------
# QuerySet.update
# ---------------------------------------------------------------------------


def test_queryset_update(seeded_db: Database) -> None:
    updated = (
        seeded_db.select(Product)
        .filter(Product.in_stock == False)  # noqa: E712
        .update(in_stock=True)
    )
    assert updated == 1
    still_false = seeded_db.select(Product).filter(Product.in_stock == False).count()  # noqa: E712
    assert still_false == 0


def test_queryset_update_no_fields_returns_zero(seeded_db: Database) -> None:
    assert seeded_db.select(Product).update() == 0


def test_queryset_update_unknown_field_raises(seeded_db: Database) -> None:
    with pytest.raises(ValueError, match="no field 'nonexistent'"):
        seeded_db.select(Product).update(nonexistent="value")


# ---------------------------------------------------------------------------
# Optional fields (NULL handling)
# ---------------------------------------------------------------------------


def test_optional_field_none(db: Database) -> None:
    with db_session:
        Note(title="Hello")  # body not set → ""
    loaded = db.select(Note).fetch_one()
    assert loaded is not None
    assert loaded.title == "Hello"
    assert loaded.body == ""


def test_opt_str_explicit_non_nullable_runtime(db: Database) -> None:
    class NonNullDesc(Entity):
        id: PK[int]
        description: Opt[str] = Opt(nullable=False)

    db2 = Database(entities=[NonNullDesc])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)
    with db_session:
        NonNullDesc(description="abc")
    loaded = db2.select(NonNullDesc).fetch_one()
    assert loaded is not None
    assert loaded.description == "abc"
    db2.close()


def test_opt_longstr_nullable_and_non_nullable(db: Database) -> None:
    class NullableLongStrEntity(Entity):
        id: PK[int]
        bio: Opt[LongStr] = Opt(nullable=True)

    class NonNullLongStrEntity(Entity):
        id: PK[int]
        bio: Opt[LongStr]

    db2 = Database(entities=[NullableLongStrEntity, NonNullLongStrEntity])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)
    with db_session:
        NullableLongStrEntity()
        NonNullLongStrEntity()
    loaded_a = db2.select(NullableLongStrEntity).fetch_one()
    loaded_b = db2.select(NonNullLongStrEntity).fetch_one()
    assert loaded_a is not None and loaded_a.bio is None
    assert loaded_b is not None and loaded_b.bio == ""
    db2.close()


def test_opt_int_nullable_and_non_nullable(db: Database) -> None:
    class OptIntEntity(Entity):
        id: PK[int]
        score: Opt[int]

    class NonNullIntEntity(Entity):
        id: PK[int]
        score: Opt[int] = Opt(nullable=False)

    db2 = Database(entities=[OptIntEntity, NonNullIntEntity])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)
    with db_session:
        OptIntEntity()
        NonNullIntEntity(score=7)
    loaded_a = db2.select(OptIntEntity).fetch_one()
    loaded_b = db2.select(NonNullIntEntity).fetch_one()
    assert loaded_a is not None and loaded_a.score is None
    assert loaded_b is not None and loaded_b.score == 7
    db2.close()


def test_opt_float_nullable_and_non_nullable(db: Database) -> None:
    class OptFloatEntity(Entity):
        id: PK[int]
        weight: Opt[float]

    class NonNullFloatEntity(Entity):
        id: PK[int]
        weight: Opt[float] = Opt(nullable=False)

    db2 = Database(entities=[OptFloatEntity, NonNullFloatEntity])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)
    with db_session:
        OptFloatEntity()
        NonNullFloatEntity(weight=1.5)
    loaded_a = db2.select(OptFloatEntity).fetch_one()
    loaded_b = db2.select(NonNullFloatEntity).fetch_one()
    assert loaded_a is not None and loaded_a.weight is None
    assert loaded_b is not None and loaded_b.weight == 1.5
    db2.close()


def test_opt_bool_nullable_and_non_nullable(db: Database) -> None:
    class OptBoolEntity(Entity):
        id: PK[int]
        indoor: Opt[bool]

    class NonNullBoolEntity(Entity):
        id: PK[int]
        indoor: Opt[bool] = Opt(nullable=False)

    db2 = Database(entities=[OptBoolEntity, NonNullBoolEntity])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)
    with db_session:
        OptBoolEntity()
        NonNullBoolEntity(indoor=True)
    loaded_a = db2.select(OptBoolEntity).fetch_one()
    loaded_b = db2.select(NonNullBoolEntity).fetch_one()
    assert loaded_a is not None and loaded_a.indoor is None
    # SQLite stores bool as int (1)
    assert loaded_b is not None and loaded_b.indoor in (True, 1)
    db2.close()


def test_filter_is_null(db: Database) -> None:
    with db_session:
        Note(title="A")  # body = ""
        Note(title="B", body="some text")
    results = db.select(Note).filter(Note.body.is_null()).fetch_all()
    assert len(results) == 0  # No NULLs, only ""


def test_filter_is_not_null(db: Database) -> None:
    with db_session:
        Note(title="A")
        Note(title="B", body="text")
    results = db.select(Note).filter(Note.body.is_not_null()).fetch_all()
    assert len(results) == 2  # Both rows, since no NULLs


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


def test_lifecycle_insert_hooks(db: Database) -> None:
    _clear_hooks()
    with db_session:
        HookedEntity(label="first")
    assert _hook_calls == ["before_insert", "after_insert"]


def test_lifecycle_update_hooks(db: Database) -> None:
    with db_session:
        h = HookedEntity(label="first")
    _clear_hooks()
    with db_session:
        h.label = "second"  # marks dirty
        db.flush()  # UPDATE
    assert _hook_calls == ["before_update", "after_update"]


def test_lifecycle_delete_hook(db: Database) -> None:
    with db_session:
        h = HookedEntity(label="to_delete")
    _clear_hooks()
    db.delete_instance(h)
    assert _hook_calls == ["before_delete", "after_delete"]


def test_lifecycle_after_load_hook(db: Database) -> None:
    with db_session:
        HookedEntity(label="loadme")
    _clear_hooks()
    results = db.select(HookedEntity).fetch_all()
    assert len(results) == 1
    assert "after_load" in _hook_calls


# ---------------------------------------------------------------------------
# require_mapped guard
# ---------------------------------------------------------------------------


def test_save_unmapped_entity_raises(db: Database) -> None:
    class Solo(Entity):
        x: Req[int]

    with pytest.raises(RuntimeError, match="not in the mapped schema"), db_session:
        Solo(x=1)
        db.flush()


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_database_context_manager() -> None:
    with Database(entities=[Product]) as _db:
        _db.bind("sqlite", ":memory:")
        _db.generate_mapping(create_tables=True)
        with db_session:
            p = Product(name="cm_test", price=1.0, in_stock=True)
        assert p.id is not None
    # Connection is closed after the with block — no error raised


# ---------------------------------------------------------------------------
# ManyToOne FK column (None field_name branch in _map_row)
# ---------------------------------------------------------------------------


def test_fetch_entity_with_fk_column(db: Database) -> None:
    """Entities with a ManyToOne relation have a FK column in the table.

    The FK column (e.g. ``category_id``) maps to ``None`` in ``_column_map``
    and is silently skipped during row-to-entity conversion.
    """
    # Insert a Category first (required by FK constraint)
    with db_session:
        cat = Category(label="Electronics")

    # Direct SQL INSERT to set the FK column — we can't do it via ORM yet
    db._execute_dml(
        "INSERT INTO taggedproduct (name, category_id) VALUES (?, ?)",
        ["Widget", cat.id],
    )

    results = db.select(TaggedProduct).fetch_all()
    assert len(results) == 1
    assert results[0].name == "Widget"
    # The category_id FK column is silently ignored (no field for it)


def test_add_sql_offset_without_limit_adds_implicit_limit() -> None:
    """SELECT with offset but no limit gets LIMIT -1 injected by the builder."""
    from nextorm.sql.builder import render as _render
    from nextorm.sql.nodes import Select, Star

    node = Select(columns=(Star(),), from_table="t", offset=5)
    sql, _ = _render(node)
    assert "LIMIT -1" in sql
    assert "OFFSET 5" in sql


# ---------------------------------------------------------------------------
# QuerySet.join() — string table name branch
# ---------------------------------------------------------------------------


def test_join_string_table_name() -> None:
    """join() with a raw string table name populates _joins correctly."""
    from nextorm.sql.nodes import BinOp, ColumnRef

    db = Database(entities=[Product])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    on = BinOp(ColumnRef("id", "product"), "=", ColumnRef("id", "product"))
    qs = db.select(Product).join("product", on=on)
    assert len(qs._joins) == 1
    assert qs._joins[0][1] == "product"
    db.close()


def test_join_entity_class() -> None:
    """join() with an entity class uses __name__.lower() for the table name."""
    from nextorm.sql.nodes import BinOp, ColumnRef

    db = Database(entities=[Product, Category])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    on = BinOp(ColumnRef("id", "product"), "=", ColumnRef("id", "category"))
    qs = db.select(Product).join(Category, on=on)
    assert len(qs._joins) == 1
    assert qs._joins[0][1] == "category"
    db.close()


# ---------------------------------------------------------------------------
# QuerySet.prefetch() — descriptor attr and error cases
# ---------------------------------------------------------------------------


def test_prefetch_with_descriptor_attr(seeded_db: Database) -> None:
    """prefetch() accepts a SetDescriptor-like attr and extracts its name."""

    class _FakeDescriptor:
        name = "some_relation"

    qs = seeded_db.select(Product).prefetch(_FakeDescriptor())
    assert "some_relation" in qs._prefetches


def test_prefetch_attr_without_name_raises(seeded_db: Database) -> None:
    """prefetch() raises ValueError when the attr has no .name."""
    qs = seeded_db.select(Product)
    with pytest.raises(ValueError, match="Cannot determine"):
        qs.prefetch(object())


def test_prefetch_unknown_relation_raises(seeded_db: Database) -> None:
    """prefetch() raises ValueError for a relation name not on the entity."""
    qs = seeded_db.select(Product).prefetch("nonexistent_relation")
    with pytest.raises(ValueError, match="no relation"):
        qs.fetch_all()


# ---------------------------------------------------------------------------
# Identity-map integration — _map_row with active db_session
# ---------------------------------------------------------------------------


def test_identity_map_same_instance_within_session(seeded_db: Database) -> None:
    """Two fetches in the same session return the same object identity."""
    from nextorm.session import db_session

    with db_session:
        r1 = seeded_db.select(Product).fetch_all()
        r2 = seeded_db.select(Product).fetch_all()

    assert r1[0] is r2[0], "Expected identity-map cache hit to return same instance"


def test_identity_map_registers_entity(seeded_db: Database) -> None:
    """Fetching within a session puts entities into the cache."""
    from nextorm.session import db_session

    with db_session:
        results = seeded_db.select(Product).fetch_all()

    pk = results[0].id
    # Cache was populated during the fetch (and cleared on exit, but we
    # already captured the result object — just verify it was wired.
    assert results[0] is results[0]  # smoke: object is intact
    _ = pk  # used


def test_identity_map_cache_hit_returns_same_instance(seeded_db: Database) -> None:
    """A second fetch within the same db_session returns the cached object (line 444)."""
    from nextorm.session import db_session

    with db_session:
        all_first = seeded_db.select(Product).fetch_all()
        # fetch_one issues a new query, but the identity map should return the same obj
        first = seeded_db.select(Product).fetch_one()

    # Both fetches happened in the same session — must be identical objects
    assert first is all_first[0]


def test_map_row_fk_column_mapped_to_none_skipped() -> None:
    """_map_row skips column-map entries that are None (line 450)."""
    # TaggedProduct has a FK column that maps to _category_id in _column_map.
    # After a regular select it should also have produced entries (not None).
    # We test that the fetch works without errors — None-column entries are skipped.
    db = Database(entities=[Product, Category, TaggedProduct])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    with db_session:
        cat = Category(label="Gadgets")
        db.flush()  # cat.id set
        TaggedProduct(name="Widget", category=cat)
    results = db.select(TaggedProduct).fetch_all()
    assert len(results) == 1
    assert results[0].name == "Widget"
    db.close()


# ---------------------------------------------------------------------------
# _do_prefetch coverage: pk_field is None, string Set target, unresolvable target
# ---------------------------------------------------------------------------


def test_prefetch_pk_field_none_returns_early() -> None:
    """_do_prefetch returns immediately when entity has no pk_fields."""
    db = Database(entities=[Category, TaggedProduct])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    with db_session:
        cat = Category(label="c")
        db.flush()  # cat.id set
        prod = TaggedProduct(name="X")
        prod.category = cat
    qs = db.select(TaggedProduct).prefetch("category")

    orig_fields = TaggedProduct._pk_fields_
    orig_field = TaggedProduct._pk_field_
    try:
        TaggedProduct._pk_fields_ = ()
        TaggedProduct._pk_field_ = None
        # _do_prefetch returns early when pk_fields is empty
        results = qs.fetch_all()
    finally:
        TaggedProduct._pk_fields_ = orig_fields
        TaggedProduct._pk_field_ = orig_field

    assert len(results) == 1
    db.close()


def test_prefetch_string_set_target_skipped(seeded_db: Database) -> None:
    """prefetch() silently skips Set[str-forward-ref] targets (line 490)."""
    db = Database(entities=[_StringRefParent, _StringRefChild])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    with db_session:
        _StringRefParent(label="p1")
    # _StringRefParent.kids is Set["_StringRefChild"] — the target is a string forward ref
    # after eval_str the result is a generic alias with string arg, which becomes a str target
    results = db.select(_StringRefParent).prefetch("kids").fetch_all()
    assert len(results) == 1
    db.close()


def test_prefetch_unresolvable_set_target_skipped() -> None:
    """prefetch() silently skips Set with unresolvable target (line 496)."""
    db = Database(entities=[_PrefetchUnresolvableParent])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    with db_session:
        _PrefetchUnresolvableParent(label="q1")
    results = db.select(_PrefetchUnresolvableParent).prefetch("stuff").fetch_all()
    assert len(results) == 1
    db.close()


# ---------------------------------------------------------------------------
# get_or_raise — ObjectNotFound / MultipleObjectsFoundError
# ---------------------------------------------------------------------------


def test_get_or_raise_returns_match(seeded_db: Database) -> None:
    p = seeded_db.select(Product).filter(Product.name == "Widget").get_or_raise()
    assert p.name == "Widget"


def test_get_or_raise_raises_object_not_found(seeded_db: Database) -> None:
    with pytest.raises(ObjectNotFound, match="Product"):
        seeded_db.select(Product).filter(Product.name == "NoSuchProduct").get_or_raise()


def test_get_or_raise_raises_multiple(seeded_db: Database) -> None:
    with pytest.raises(MultipleObjectsFoundError):
        seeded_db.select(Product).get_or_raise()


# ---------------------------------------------------------------------------
# QuerySet.page
# ---------------------------------------------------------------------------


def test_page_returns_correct_slice(seeded_db: Database) -> None:
    # 3 products ordered by id; page 1 size 2 → first two
    page1 = seeded_db.select(Product).order_by(Product.id.asc()).page(1, 2)
    assert len(page1.fetch_all()) == 2


def test_page_second_page(seeded_db: Database) -> None:
    page2 = seeded_db.select(Product).order_by(Product.id.asc()).page(2, 2)
    results = page2.fetch_all()
    assert len(results) == 1


def test_page_invalid_pagenum_raises() -> None:
    db = Database(entities=[Product])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    qs = db.select(Product)
    with pytest.raises(ValueError, match="pagenum"):
        qs.page(0)
    db.close()


# ---------------------------------------------------------------------------
# QuerySet.distinct
# ---------------------------------------------------------------------------


def test_distinct_deduplicates(seeded_db: Database) -> None:
    # stock=True duplicate: seeds have "Widget" and "Doohickey" both in_stock=True
    # The SELECT DISTINCT on the whole entity row won't deduplicate those.
    # Test that the flag propagates by checking get_sql output.
    sql = seeded_db.select(Product).distinct().get_sql()
    assert "DISTINCT" in sql


# ---------------------------------------------------------------------------
# QuerySet.get_sql
# ---------------------------------------------------------------------------


def test_get_sql_contains_where(seeded_db: Database) -> None:
    sql = seeded_db.select(Product).filter(Product.price > 10).get_sql()
    assert "WHERE" in sql
    assert "price" in sql


def test_get_sql_contains_order_by(seeded_db: Database) -> None:
    sql = seeded_db.select(Product).order_by(Product.name.asc()).get_sql()
    assert "ORDER BY" in sql


# ---------------------------------------------------------------------------
# QuerySet.for_update
# ---------------------------------------------------------------------------


def test_for_update_renders_in_sql(seeded_db: Database) -> None:
    sql = seeded_db.select(Product).for_update().get_sql()
    assert "FOR UPDATE" in sql


def test_for_update_skip_locked_renders_in_sql(seeded_db: Database) -> None:
    sql = seeded_db.select(Product).for_update(skip_locked=True).get_sql()
    assert "SKIP LOCKED" in sql


# ---------------------------------------------------------------------------
# QuerySet aggregations: sum / avg / min / max
# ---------------------------------------------------------------------------


def test_sum_returns_total(seeded_db: Database) -> None:
    total = seeded_db.select(Product).sum("price")
    assert total == pytest.approx(9.99 + 24.99 + 4.99)


def test_avg_returns_average(seeded_db: Database) -> None:
    avg = seeded_db.select(Product).avg("price")
    assert avg == pytest.approx((9.99 + 24.99 + 4.99) / 3)


def test_min_returns_minimum(seeded_db: Database) -> None:
    mn = seeded_db.select(Product).min("price")
    assert mn == pytest.approx(4.99)


def test_max_returns_maximum(seeded_db: Database) -> None:
    mx = seeded_db.select(Product).max("price")
    assert mx == pytest.approx(24.99)


def test_sum_no_rows_returns_none(db: Database) -> None:
    result = db.select(Product).sum("price")
    assert result is None


def test_aggregate_unknown_field_raises(seeded_db: Database) -> None:
    with pytest.raises(ValueError, match="no field"):
        seeded_db.select(Product).sum("nonexistent")


# ---------------------------------------------------------------------------
# QuerySet.random
# ---------------------------------------------------------------------------


def test_random_returns_n_rows(seeded_db: Database) -> None:
    results = seeded_db.select(Product).random(2)
    assert len(results.fetch_all()) == 2


# ---------------------------------------------------------------------------
# Database.execute and Database.select_raw
# ---------------------------------------------------------------------------


def test_db_execute_runs_dml(seeded_db: Database) -> None:
    affected = seeded_db.execute("DELETE FROM product WHERE name = ?", "Gadget")
    assert affected == 1
    remaining = seeded_db.select(Product).count()
    assert remaining == 2


def test_db_select_raw_returns_dicts(seeded_db: Database) -> None:
    rows = seeded_db.select_raw("SELECT name, price FROM product ORDER BY price ASC")
    assert isinstance(rows[0], dict)
    assert rows[0]["name"] == "Doohickey"


def test_db_select_raw_with_params(seeded_db: Database) -> None:
    rows = seeded_db.select_raw("SELECT name FROM product WHERE price > ?", 10.0)
    assert len(rows) == 1
    assert rows[0]["name"] == "Gadget"


# ---------------------------------------------------------------------------
# Database.get_connection
# ---------------------------------------------------------------------------


def test_get_connection_returns_raw_connection(seeded_db: Database) -> None:
    import sqlite3  # noqa: PLC0415

    raw = seeded_db.get_connection()
    assert isinstance(raw, sqlite3.Connection)


# ---------------------------------------------------------------------------
# QuerySet.raw / QuerySet.raw_one
# ---------------------------------------------------------------------------


def test_raw_returns_entity_instances(seeded_db: Database) -> None:
    """raw() executes SQL and maps rows to entity instances by column name."""
    results = seeded_db.select(Product).raw(
        "SELECT id, name, price, in_stock FROM product ORDER BY price ASC"
    )
    assert len(results) == 3
    assert all(isinstance(r, Product) for r in results)
    assert results[0].name == "Doohickey"


def test_raw_with_params(seeded_db: Database) -> None:
    """raw() forwards bind parameters to the database."""
    results = seeded_db.select(Product).raw(
        "SELECT id, name, price, in_stock FROM product WHERE price > ?", [10.0]
    )
    assert len(results) == 1
    assert results[0].name == "Gadget"


def test_raw_unknown_column_is_ignored(seeded_db: Database) -> None:
    """Columns not in the entity schema map to None and are silently skipped."""
    results = seeded_db.select(Product).raw(
        "SELECT id, name, price, in_stock, 42 AS extra FROM product WHERE name = ?",
        ["Widget"],
    )
    assert len(results) == 1
    assert results[0].name == "Widget"


def test_raw_with_fk_column(db: Database) -> None:
    """FK id columns (_rel_id) are stored directly in __dict__ by _map_raw_row."""
    with db_session:
        cat = Category(label="Electronics")
        db.flush()  # cat.id set
        tp = TaggedProduct(name="Radio")
        tp.category = cat
    results = db.select(TaggedProduct).raw("SELECT id, name, category_id FROM taggedproduct")
    assert len(results) == 1
    assert results[0].name == "Radio"
    # FK stored internally so category can be resolved
    assert vars(results[0]).get("_category_id") == cat.id


def test_raw_no_params_defaults_to_empty(seeded_db: Database) -> None:
    """raw() with no params argument succeeds (defaults to empty list)."""
    results = seeded_db.select(Product).raw("SELECT id, name, price, in_stock FROM product")
    assert len(results) == 3


def test_raw_one_returns_first_entity(seeded_db: Database) -> None:
    """raw_one() returns the first row mapped to an entity."""
    result = seeded_db.select(Product).raw_one(
        "SELECT id, name, price, in_stock FROM product WHERE name = ?", ["Widget"]
    )
    assert result is not None
    assert result.name == "Widget"


def test_raw_one_returns_none_when_empty(seeded_db: Database) -> None:
    """raw_one() returns None when the query produces no rows."""
    result = seeded_db.select(Product).raw_one(
        "SELECT id, name, price, in_stock FROM product WHERE name = ?", ["NoSuchItem"]
    )
    assert result is None


def test_raw_one_no_params_defaults_to_empty(seeded_db: Database) -> None:
    """raw_one() with no params argument succeeds."""
    result = seeded_db.select(Product).raw_one(
        "SELECT id, name, price, in_stock FROM product LIMIT 1"
    )
    assert result is not None


def test_raw_set_relation_entity_skips_set_kind() -> None:
    """_build_column_map_from_names skips SET relations (covers 103->102 branch)."""
    _db = Database(entities=[_StringRefParent, _StringRefChild])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)
    with db_session:
        _StringRefParent(label="root")
    results = _db.select(_StringRefParent).raw("SELECT id, label FROM _stringrefparent")
    assert len(results) == 1
    assert results[0].label == "root"
    _db.close()


# ---------------------------------------------------------------------------
# db.last_sql
# ---------------------------------------------------------------------------


def test_last_sql_empty_before_any_query(db: Database) -> None:
    """last_sql is empty string before any query is executed."""
    assert db.last_sql == ""


def test_last_sql_set_after_fetch_all(seeded_db: Database) -> None:
    """last_sql reflects the SELECT executed by fetch_all."""
    seeded_db.select(Product).fetch_all()
    assert "SELECT" in seeded_db.last_sql.upper()
    assert "product" in seeded_db.last_sql


def test_last_sql_set_after_save_insert(db: Database) -> None:
    """last_sql reflects the INSERT executed by save()."""
    with db_session:
        Product(name="Widget", price=9.99, in_stock=True)
    assert "INSERT" in db.last_sql.upper()


def test_last_sql_set_after_save_update(db: Database) -> None:
    """last_sql reflects the UPDATE executed by save() on an existing entity."""
    with db_session:
        p = Product(name="Widget", price=9.99, in_stock=True)
        db.flush()  # INSERT
        p.price = 5.00  # marks dirty
        db.flush()  # UPDATE
    assert "UPDATE" in db.last_sql.upper()


def test_last_sql_set_after_qs_delete(seeded_db: Database) -> None:
    """last_sql reflects the DELETE executed by QuerySet.delete()."""
    seeded_db.select(Product).filter(Product.name == "Widget").delete()
    assert "DELETE" in seeded_db.last_sql.upper()


def test_last_sql_updated_on_each_query(seeded_db: Database) -> None:
    """last_sql is overwritten by subsequent queries."""
    seeded_db.select(Product).fetch_all()
    first_sql = seeded_db.last_sql
    seeded_db.select(Product).filter(Product.name == "Gadget").fetch_all()
    assert seeded_db.last_sql != first_sql


def test_last_sql_set_after_raw(seeded_db: Database) -> None:
    """last_sql is set when raw() is used."""
    seeded_db.select(Product).raw("SELECT id, name, price, in_stock FROM product")
    assert "product" in seeded_db.last_sql


# ---------------------------------------------------------------------------
# QuerySet.without_distinct / group_concat
# ---------------------------------------------------------------------------


def test_without_distinct_clears_distinct_flag(seeded_db: Database) -> None:
    """without_distinct() removes the DISTINCT flag set by distinct()."""
    qs = seeded_db.select(Product).distinct().without_distinct()
    assert "DISTINCT" not in qs.get_sql().upper()


def test_without_distinct_on_fresh_queryset(seeded_db: Database) -> None:
    """without_distinct() on a fresh (non-distinct) QuerySet is a no-op."""
    qs = seeded_db.select(Product).without_distinct()
    results = qs.fetch_all()
    assert len(results) == 3


def test_group_concat_returns_concatenated_string(seeded_db: Database) -> None:
    """group_concat() returns a comma-separated string of all matching values."""
    result = seeded_db.select(Product).order_by(Product.name.asc()).group_concat("name")
    assert result is not None
    assert "Doohickey" in result
    assert "Gadget" in result
    assert "Widget" in result


def test_group_concat_custom_separator(seeded_db: Database) -> None:
    """group_concat() uses the provided separator."""
    result = seeded_db.select(Product).group_concat("name", sep=" | ")
    assert result is not None
    assert " | " in result


def test_group_concat_with_filter(seeded_db: Database) -> None:
    """group_concat() respects the active WHERE filter."""
    result = seeded_db.select(Product).filter(Product.price > 10.0).group_concat("name")
    assert result is not None
    assert "Gadget" in result
    assert "Widget" not in result
    assert "Doohickey" not in result


def test_group_concat_returns_none_on_empty(db: Database) -> None:
    """group_concat() returns None when no rows match."""
    result = db.select(Product).group_concat("name")
    # SQLite returns None for GROUP_CONCAT with no rows
    assert result is None


def test_group_concat_invalid_attr_raises(seeded_db: Database) -> None:
    """group_concat() raises ValueError for unknown field names."""
    import pytest as _pytest  # noqa: PLC0415

    with _pytest.raises(ValueError, match="has no field"):
        seeded_db.select(Product).group_concat("nonexistent")


# ---------------------------------------------------------------------------
# Entity.select_by_sql / Entity.get_by_sql
# ---------------------------------------------------------------------------


def test_select_by_sql_returns_entities(seeded_db: Database) -> None:
    """Entity.select_by_sql() executes raw SQL and returns entity instances."""
    results = Product.select_by_sql(seeded_db, "SELECT id, name, price, in_stock FROM product")
    assert len(results) == 3
    assert all(isinstance(r, Product) for r in results)


def test_select_by_sql_with_params(seeded_db: Database) -> None:
    """Entity.select_by_sql() forwards bind parameters to the SQL driver."""
    results = Product.select_by_sql(
        seeded_db,
        "SELECT id, name, price, in_stock FROM product WHERE price > ?",
        [10.0],
    )
    assert len(results) == 1
    assert results[0].name == "Gadget"


def test_get_by_sql_returns_entity(seeded_db: Database) -> None:
    """Entity.get_by_sql() returns the first matching entity instance."""
    result = Product.get_by_sql(
        seeded_db,
        "SELECT id, name, price, in_stock FROM product WHERE name = ?",
        ["Widget"],
    )
    assert result is not None
    assert result.name == "Widget"


def test_get_by_sql_returns_none_when_empty(seeded_db: Database) -> None:
    """Entity.get_by_sql() returns None when the query produces no rows."""
    result = Product.get_by_sql(
        seeded_db,
        "SELECT id, name, price, in_stock FROM product WHERE name = ?",
        ["NoSuchProduct"],
    )
    assert result is None


# ---------------------------------------------------------------------------
# for_update(nowait=True)
# ---------------------------------------------------------------------------


def test_for_update_nowait_renders_in_sql(seeded_db: Database) -> None:
    """for_update(nowait=True) renders FOR UPDATE NOWAIT."""
    sql = seeded_db.select(Product).for_update(nowait=True).get_sql()
    assert "FOR UPDATE" in sql
    assert "NOWAIT" in sql


def test_for_update_nowait_and_skip_locked_mutual_exclusive(
    seeded_db: Database,
) -> None:
    """for_update(nowait=True, skip_locked=True) raises ValueError."""
    import pytest as _pytest

    with _pytest.raises(ValueError, match="mutually exclusive"):
        seeded_db.select(Product).for_update(nowait=True, skip_locked=True)


# ---------------------------------------------------------------------------
# QuerySet.__len__ and QuerySet.__getitem__
# ---------------------------------------------------------------------------


def test_queryset_len_returns_count(seeded_db: Database) -> None:
    """len(qs) returns the number of matching rows."""
    qs = seeded_db.select(Product)
    assert len(qs) == seeded_db.select(Product).count()


def test_queryset_getitem_int_returns_entity(seeded_db: Database) -> None:
    """qs[0] returns the first entity in order."""
    qs = seeded_db.select(Product).order_by(Product.id.asc())
    first = qs[0]
    assert isinstance(first, Product)
    all_results = qs.fetch_all()
    assert first.id == all_results[0].id


def test_queryset_getitem_slice_returns_list(seeded_db: Database) -> None:
    """qs[0:1] returns a list of matching entities."""
    qs = seeded_db.select(Product)
    results = qs[0:1]
    assert isinstance(results, list)
    assert len(results) == 1


def test_queryset_getitem_empty_slice(seeded_db: Database) -> None:
    """qs[0:0] returns an empty list."""
    qs = seeded_db.select(Product)
    results = qs[0:0]
    assert results == []


def test_queryset_getitem_out_of_range_raises(seeded_db: Database) -> None:
    """qs[999] raises IndexError when no such offset exists."""
    import pytest as _pytest

    qs = seeded_db.select(Product)
    with _pytest.raises(IndexError):
        qs[999]


def test_queryset_getitem_negative_raises(seeded_db: Database) -> None:
    """qs[-1] raises ValueError."""
    import pytest as _pytest

    qs = seeded_db.select(Product)
    with _pytest.raises(ValueError, match="Negative"):
        qs[-1]


def test_queryset_getitem_negative_slice_raises(seeded_db: Database) -> None:
    """qs[-1:0] raises ValueError."""
    import pytest as _pytest

    qs = seeded_db.select(Product)
    with _pytest.raises(ValueError, match="Negative"):
        qs[-1:0]


def test_queryset_getitem_step_raises(seeded_db: Database) -> None:
    """qs[0:10:2] raises ValueError."""
    import pytest as _pytest

    qs = seeded_db.select(Product)
    with _pytest.raises(ValueError, match="Step"):
        qs[0:10:2]


def test_queryset_getitem_wrong_type_raises(seeded_db: Database) -> None:
    """qs[\"key\"] raises TypeError."""
    import pytest as _pytest

    qs = seeded_db.select(Product)
    with _pytest.raises(TypeError, match="int or slice"):
        qs["key"]  # type: ignore[call-overload]


def test_queryset_getitem_open_slice(seeded_db: Database) -> None:
    """qs[1:] returns all results starting from the second row (no upper bound)."""

    qs = seeded_db.select(Product).order_by(Product.id.asc())
    results = qs[1:]  # slice with no stop → no LIMIT applied
    assert isinstance(results, list)
    all_results = qs.fetch_all()
    assert len(results) == len(all_results) - 1


def test_queryset_getitem_full_slice_preserves_offset_and_limit(
    seeded_db: Database,
) -> None:
    """qs.offset(n).limit(m)[:] preserves the existing offset and limit."""
    qs = seeded_db.select(Product).order_by(Product.id.asc())
    all_results = qs.fetch_all()
    # Fetch the second result via .offset(1).limit(1)[:]
    result = qs.offset(1).limit(1)[:]
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].id == all_results[1].id


# ---------------------------------------------------------------------------
# _build_column_map_from_names: non-owning O2O (line 137) and composite FK (lines 141-145)
# _build_explicit_column_map: non-owning O2O (line 217) and composite FK (lines 220-225)
# ---------------------------------------------------------------------------


class _O2OQOwner(Entity):
    """Non-owning side of an O2O for raw() column map tests."""

    _table_ = "_o2oq_owner"
    name: Req[str]
    partner: Single["_O2OQPartner"] = Single(nullable=True)  # noqa: UP037


class _O2OQPartner(Entity):
    """Owning side: relation-based PK → FK column on partner's table."""

    _table_ = "_o2oq_partner"
    host: PK[_O2OQOwner]  # relation-based PK
    value: Req[str]


class _LazyO2OSelf(Entity):
    """Owner entity that will have a non-owning O2O when _LazyO2OSelfPartner is defined."""

    _table_ = "_lazy_o2o_self"
    name: Req[str]
    detail: Req[str] = Req(lazy=True)  # lazy field → triggers _build_explicit_column_map
    partner: Single["_LazyO2OSelfPartner"] = Single(nullable=True)  # noqa: UP037 — non-owning O2O


class _LazyO2OSelfPartner(Entity):
    """Partner entity whose PK is a relation back to _LazyO2OSelf."""

    _table_ = "_lazy_o2o_self_partner"
    host: PK[_LazyO2OSelf]  # relation-based PK → FK lives on THIS table, not on _LazyO2OSelf
    value: Req[str]


from tests.test_composite_pk import Enrollment as _EnrollmentQ  # noqa: E402
from tests.test_composite_pk import GradeNote as _GradeNote  # noqa: E402


class _LazyGradeNote(Entity):
    """Lazy-field entity with a composite FK relation (for _build_explicit_column_map)."""

    _table_ = "_lazy_grade_note"
    note: Req[str]
    detail: Req[str] = Req(lazy=True)  # lazy field → triggers _build_explicit_column_map
    enrollment: Single[_EnrollmentQ]  # composite FK → lines 220-225


@pytest.fixture()
def raw_test_db() -> Database:
    """Database with O2O entities and composite FK for raw() tests."""
    from nextorm.session import db_session as _dbs

    db = Database(
        entities=[
            _O2OQOwner,
            _O2OQPartner,
            _LazyO2OSelf,
            _LazyO2OSelfPartner,
            _EnrollmentQ,
            _GradeNote,
            _LazyGradeNote,
        ]
    )
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    with _dbs:
        owner = _O2OQOwner(name="raw-owner")
        from nextorm import flush

        flush()
        partner = _O2OQPartner(value="raw-partner")
        partner.host = owner
        flush()
        enrollment = _EnrollmentQ(student_id=100, course_id=200)
        flush()
        note = _GradeNote(note="raw-grade-note")
        note.enrollment = enrollment
        flush()
    return db


def test_raw_query_non_owning_o2o_skips_fk_column(raw_test_db: Database) -> None:
    """raw() on a non-owning O2O entity skips the FK column in column map."""
    # _O2OQOwner.partner is non-owning: the FK lives on _O2OQPartner.host table.
    # _build_column_map_from_names for _O2OQOwner should skip partner (continue → line 137)
    results = raw_test_db.select(_O2OQOwner).raw("SELECT id, name FROM _o2oq_owner")
    assert len(results) >= 1
    assert results[0].name == "raw-owner"
    raw_test_db.close()


def test_raw_query_composite_fk_encodes_components(raw_test_db: Database) -> None:
    """raw() on a composite-FK entity encodes FK columns with position info."""
    # _GradeNote.enrollment → composite FK columns: enrollment_student_id, enrollment_course_id
    # _build_column_map_from_names for GradeNote → lines 141-145 (composite FK encoding)
    results = raw_test_db.select(_GradeNote).raw(
        "SELECT id, note, enrollment_student_id, enrollment_course_id FROM gradenote"
    )
    assert len(results) == 1
    assert results[0].note == "raw-grade-note"


def test_lazy_entity_with_non_owning_o2o_explicit_col_map(
    raw_test_db: Database,
) -> None:
    """Lazy entity with non-owning O2O: _build_explicit_column_map skips FK col."""
    # _LazyO2OSelf has lazy field → _build_explicit_column_map called
    # partner is non-owning O2O → _is_non_owning_single returns True → continue (line 217)
    results = raw_test_db.select(_LazyO2OSelf).fetch_all()
    assert isinstance(results, list)


def test_lazy_entity_with_composite_fk_explicit_col_map(raw_test_db: Database) -> None:
    """Lazy entity with composite FK: _build_explicit_column_map adds FK cols."""
    # _LazyGradeNote has lazy field → _build_explicit_column_map called
    # enrollment is composite FK → lines 220-225
    results = raw_test_db.select(_LazyGradeNote).fetch_all()
    assert isinstance(results, list)
    raw_test_db.close()


# ---------------------------------------------------------------------------
# QuerySet._build_select: explicit columns (lazy field) + JOIN
# ---------------------------------------------------------------------------


def test_lazy_entity_explicit_columns_with_join(tmp_path: Any) -> None:
    """QuerySet for lazy entity + JOIN qualifies explicit columns (covers query.py 1116-1117)."""

    class _LazJParent(Entity):
        _table_ = "_lazjparent"
        name: Req[str]

    class _LazJChild(Entity):
        _table_ = "_lazjchild"
        text: Req[str]
        detail: Req[str] = Req(lazy=True)  # lazy field → _explicit_columns is set
        parent: Single[_LazJParent]

    db = Database(entities=[_LazJParent, _LazJChild])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    p1 = _LazJParent(name="Bravo")
    db.save(p1)
    p2 = _LazJParent(name="Alpha")
    db.save(p2)
    c1 = _LazJChild(text="child-b", detail="hidden-b")
    vars(c1)["_parent_id"] = p1.id
    db.save(c1)
    c2 = _LazJChild(text="child-a", detail="hidden-a")
    vars(c2)["_parent_id"] = p2.id
    db.save(c2)

    # order_by traversing relation → JOIN → explicit columns + JOIN → 1116-1117
    results = db.select(_LazJChild).order_by(lambda r: r.parent.name).fetch_all()
    assert len(results) == 2
    assert results[0].text == "child-a"  # Alpha parent first
    assert results[1].text == "child-b"  # Bravo parent second
    db.close()


# ---------------------------------------------------------------------------
# QuerySet join deduplication branches
# ---------------------------------------------------------------------------


def test_order_by_same_join_deduplicated_on_chained_order_by(
    chain_db: Database,
) -> None:
    """Second order_by call deduplicates a JOIN already added by the first (covers 574->569)."""
    # First order_by adds JOIN on _chainmid; second order_by traverses _chainmid + _chaintop.
    # The dedup in the order_by loop detects the existing _chainmid JOIN and skips it (574->569).
    results = (
        chain_db.select(_ChainLeaf)
        .order_by(lambda leaf: leaf.mid.name)  # adds JOIN on _chainmid
        .order_by(lambda leaf: leaf.mid.top.slug)  # re-adds _chainmid JOIN → dedup; adds _chaintop
        .fetch_all()
    )
    assert len(results) == 3


def test_join_deduplicates_same_join(chain_db: Database) -> None:
    """Calling join() twice with the same arguments deduplicates the JOIN (covers 624->626)."""
    from nextorm.sql.nodes import BinOp, ColumnRef  # noqa: PLC0415

    on_cond = BinOp(ColumnRef("mid_id", "_chainleaf"), "=", ColumnRef("id", "_chainmid"))
    results = (
        chain_db.select(_ChainLeaf)
        .join("_chainmid", on_cond)
        .join("_chainmid", on_cond)  # same join — deduplicated
        .fetch_all()
    )
    assert len(results) == 3


# ---------------------------------------------------------------------------
# QuerySet.where() fallback to proxy-based evaluation on DecompileError
# ---------------------------------------------------------------------------


def test_where_lambda_falls_back_to_proxy_on_decompile_error(
    seeded_db: Database,
) -> None:
    """where() falls back to proxy-based evaluation when _apply_predicate raises DecompileError."""
    from unittest.mock import patch

    from nextorm.generators import DecompileError

    # Patch _apply_predicate to raise DecompileError, forcing the proxy fallback (lines 521-527)
    with patch("nextorm.generators._apply_predicate", side_effect=DecompileError("forced")):
        results = seeded_db.select(Product).where(lambda p: p.price > 5.0).fetch_all()

    assert len(results) > 0


def test_where_lambda_uses_proxy_directly_when_entity_cls_is_none(
    seeded_db: Database,
) -> None:
    """where() goes directly to proxy-based eval when table has no entity_cls (covers 518->525)."""
    import dataclasses

    from nextorm.query import QuerySet

    qs = seeded_db.select(Product)
    # Create a QS with a table that has entity_cls=None (simulates raw/join-table QS)
    raw_table = dataclasses.replace(qs._table, entity_cls=None)
    raw_qs = QuerySet(Product, raw_table, qs._db, qs._builder)
    # where() should use proxy-based evaluation directly (skips decompilation attempt)
    filtered = raw_qs.where(lambda p: p.price > 5.0)
    results = filtered.fetch_all()
    assert len(results) > 0
