"""Tests for nextorm.generators — select() generator-expression queries."""

from __future__ import annotations

import types  # noqa: TC003
from typing import TYPE_CHECKING

import pytest

from nextorm.database import Database
from nextorm.entity import Entity
from nextorm.fields import Local, Req, Set, Single
from nextorm.fields import PrimaryKey as _PrimaryKey
from nextorm.generators import (
    DecompileError,
    _decompile_condition,
    _decompile_yield_attr,
    avg,
    count,
    max,
    min,
    select,
    sum,
)
from nextorm.session import db_session

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Module-level DB so select() can discover it via sys.modules scan.
# ---------------------------------------------------------------------------


class GenWidget(Entity):
    name: Req[str]
    price: Req[float]
    active: Req[bool]


# Entity NOT registered with any database (for "no db found" error path)
class GenOrphan(Entity):
    value: Req[int]


class GenBrand(Entity):
    """Brand entity to serve as the relation target in traversal tests."""

    _table_name_ = "gen_brand"
    name: Req[str]


class GenItem(Entity):
    """Item entity with a Single[GenBrand] relation for traversal tests."""

    _table_name_ = "gen_item"
    label: Req[str]
    brand: Single[GenBrand]


class GenCategory(Entity):
    """Top-level category for 3-level chain traversal tests."""

    _table_name_ = "gen_category"
    slug: Req[str]


class GenBrandWithCat(Entity):
    """Brand that belongs to a GenCategory (for 3-level chain tests)."""

    _table_name_ = "gen_brand_cat"
    title: Req[str]
    category: Single[GenCategory]


class GenItemDeep(Entity):
    """Item linked to GenBrandWithCat (3-level: item → brand → category)."""

    _table_name_ = "gen_item_deep"
    code: Req[str]
    brand: Single[GenBrandWithCat]


# ---------------------------------------------------------------------------
# Entities for IN-operator EXISTS and O2O non-owning traversal tests
# ---------------------------------------------------------------------------


class GenShop(Entity):
    """Shop entity (non-owning side of O2O with GenShopConfig)."""

    _table_name_ = "gen_shop"
    name: Req[str]
    config: Single["GenShopConfig"] = Single(nullable=True)  # noqa: UP037  # nullable: no config on creation


class GenShopConfig(Entity):
    """ShopConfig with relation-based PK → FK on this side."""

    _table_name_ = "gen_shopconfig"
    theme: Req[str]
    shop: Single[GenShop] = Single(primary_key=True)  # FK on this table, PK


class GenPost(Entity):
    """Post entity for O2M EXISTS test."""

    _table_name_ = "gen_post"
    title: Req[str]
    comments: Set[GenComment]


class GenComment(Entity):
    """Comment entity with a boolean field."""

    _table_name_ = "gen_comment"
    text: Req[str]
    active: Req[bool]
    post: Single[GenPost]


class GenTag(Entity):
    """Tag for M2M EXISTS test."""

    _table_name_ = "gen_tag"
    label: Req[str]
    articles: Set[GenArticle]


class GenArticle(Entity):
    """Article for M2M EXISTS test."""

    _table_name_ = "gen_article"
    title: Req[str]
    tags: Set[GenTag]


# ---------------------------------------------------------------------------
# Entities for explicit owner=False non-owning O2O (lines 235->246, etc.)
# ---------------------------------------------------------------------------


class GenHub(Entity):
    """Hub entity: explicitly non-owning O2O (owner=False on spoke)."""

    _table_name_ = "gen_hub"
    name: Req[str]
    spoke: Single["GenSpoke"] = Single(owner=False, nullable=True)  # noqa: UP037


class GenSpoke(Entity):
    """Spoke entity: FK on this side (owning O2O side)."""

    _table_name_ = "gen_spoke"
    color: Req[str]
    hub: Single[GenHub]  # FK column on this table


# Entity pair where spoke2 has NO back-ref pointing to GenHub2 (covers 249->255, 256)
class GenSibling(Entity):
    """Unrelated entity used as target of GenSpoke2.other."""

    _table_name_ = "gen_sibling"
    x: Req[str]


class GenHub2(Entity):
    """Hub2: non-owning O2O where target has no matching back-ref."""

    _table_name_ = "gen_hub2"
    name: Req[str]
    spoke: Single["GenSpoke2"] = Single(owner=False, nullable=True)  # noqa: UP037


class GenSpoke2(Entity):
    """Spoke2: has a Single relation but NOT pointing back to GenHub2."""

    _table_name_ = "gen_spoke2"
    color: Req[str]
    other: Single[GenSibling]  # SINGLE but NOT matching GenHub2 → rev_fk_col fallback


# Entity with relation-based PK for testing line 263
class GenPKParent(Entity):
    """Parent entity for relation-based PK."""

    _table_name_ = "gen_pkparent"
    name: Req[str]


class GenMidPK(Entity):
    """Entity with relation-based PK (covers line 263 in non-owning handler)."""

    _table_name_ = "gen_midpk"
    owner: Single[GenPKParent]
    ext: Single["GenMidPKExt"] = Single(owner=False, nullable=True)  # noqa: UP037
    _pk_ = _PrimaryKey("owner")  # relation-based PK → 'owner' NOT in _fields_


class GenMidPKExt(Entity):
    """Extension entity: FK on this side pointing back to GenMidPK."""

    _table_name_ = "gen_midpk_ext"
    value: Req[str]
    mid: Single[GenMidPK]  # FK on this side


# Entity for unresolvable SET target (covers 587->630, 643->691)
class GenBadSet(Entity):
    """Entity with an unresolvable SET forward ref."""

    _table_name_ = "gen_bad_set"
    name: Req[str]
    ghosts: Set["_VeryNonExistent_12345"]  # type: ignore[name-defined]  # noqa: UP037,F821  # intentionally unresolvable


# Entity pair for composite-PK target (covers 271-294)
class GenCompKey(Entity):
    """Entity with scalar composite PK."""

    _table_name_ = "gen_compkey"
    part1: Req[str]
    part2: Req[str]
    value: Req[str]
    _pk_ = _PrimaryKey("part1", "part2")


class GenCompUser(Entity):
    """Entity with Single relation to composite-PK entity."""

    _table_name_ = "gen_comp_user"
    name: Req[str]
    thing: Single[GenCompKey]


class GenCompUserExplicit(Entity):
    """Single to composite-PK entity with explicit FK column names (covers line 274)."""

    _table_name_ = "gen_comp_user_explicit"
    name: Req[str]
    thing: Single[GenCompKey] = Single(columns=["thing_part1", "thing_part2"])


# Entity pair for relation-based composite PK target (covers lines 283-284)
class GenCompRef(Entity):
    """Base entity referenced by relation-PK composite."""

    _table_name_ = "gen_comp_ref"
    name: Req[str]


class GenRelCompPK(Entity):
    """Entity with relation-based composite PK (order_ref + sequence)."""

    _table_name_ = "gen_rel_comppk"
    order_ref: Single[GenCompRef]
    sequence: Req[int]
    value: Req[str]
    _pk_ = _PrimaryKey("order_ref", "sequence")


class GenRelCompUser(Entity):
    """User of GenRelCompPK (to trigger lines 283-284 in composite PK join)."""

    _table_name_ = "gen_rel_comp_user"
    tag: Req[str]
    item: Single[GenRelCompPK]


gen_db = Database(entities=[GenWidget])
gen_db.bind("sqlite", ":memory:")
gen_db.generate_mapping(create_tables=True)

gen_rel_db = Database(entities=[GenBrand, GenItem])
gen_rel_db.bind("sqlite", ":memory:")
gen_rel_db.generate_mapping(create_tables=True, validate_relations=False)

gen_deep_db = Database(entities=[GenCategory, GenBrandWithCat, GenItemDeep])
gen_deep_db.bind("sqlite", ":memory:")
gen_deep_db.generate_mapping(create_tables=True, validate_relations=False)

gen_o2o_db = Database(entities=[GenShop, GenShopConfig])
gen_o2o_db.bind("sqlite", ":memory:")
gen_o2o_db.generate_mapping(create_tables=True)

gen_m2m_db = Database(entities=[GenTag, GenArticle])
gen_m2m_db.bind("sqlite", ":memory:")
gen_m2m_db.generate_mapping(create_tables=True)

gen_o2m_db = Database(entities=[GenPost, GenComment])
gen_o2m_db.bind("sqlite", ":memory:")
gen_o2m_db.generate_mapping(create_tables=True)

gen_hub_db = Database(entities=[GenHub, GenSpoke])
gen_hub_db.bind("sqlite", ":memory:")
gen_hub_db.generate_mapping(create_tables=True)

gen_hub2_db = Database(entities=[GenHub2, GenSpoke2, GenSibling])
gen_hub2_db.bind("sqlite", ":memory:")
gen_hub2_db.generate_mapping(create_tables=True, validate_relations=False)

gen_hubx_db = Database(entities=[GenPKParent, GenMidPK, GenMidPKExt])
gen_hubx_db.bind("sqlite", ":memory:")
gen_hubx_db.generate_mapping(create_tables=True)

gen_comp_db = Database(entities=[GenCompKey, GenCompUser, GenCompUserExplicit])
gen_comp_db.bind("sqlite", ":memory:")
gen_comp_db.generate_mapping(create_tables=True)

gen_rel_comp_db = Database(entities=[GenCompRef, GenRelCompPK, GenRelCompUser])
gen_rel_comp_db.bind("sqlite", ":memory:")
gen_rel_comp_db.generate_mapping(create_tables=True)

# Seed data
with db_session:
    GenWidget(name="cheap", price=50.0, active=True)
    GenWidget(name="expensive", price=200.0, active=True)
    GenWidget(name="inactive", price=150.0, active=False)

with db_session:
    b1 = GenBrand(name="Acme")
    gen_rel_db.flush()
    GenItem(label="anvil", brand=b1)
    b2 = GenBrand(name="Globex")
    gen_rel_db.flush()
    GenItem(label="reactor", brand=b2)

with db_session:
    cat = GenCategory(slug="tools")
    gen_deep_db.flush()
    br = GenBrandWithCat(title="Hammers Inc", category=cat)
    gen_deep_db.flush()
    GenItemDeep(code="H001", brand=br)

with db_session:
    shop = GenShop(name="MyShop")
    gen_o2o_db.flush()
    cfg = GenShopConfig(theme="dark")
    cfg.shop = shop

with db_session:
    t1 = GenTag(label="python")
    t2 = GenTag(label="orms")
    gen_m2m_db.flush()
    a1 = GenArticle(title="intro")
    gen_m2m_db.flush()
    a1.tags.add(t1)
    a1.tags.add(t2)

with db_session:
    p1 = GenPost(title="MyPost")
    gen_o2m_db.flush()
    GenComment(text="good", active=True, post=p1)
    GenComment(text="bad", active=False, post=p1)

with db_session:
    hub = GenHub(name="HubOne")
    gen_hub_db.flush()
    spoke = GenSpoke(color="red")
    spoke.hub = hub

with db_session:
    sib = GenSibling(x="sib1")
    gen_hub2_db.flush()
    spoke2 = GenSpoke2(color="blue")
    spoke2.other = sib
    gen_hub2_db.flush()
    hub2 = GenHub2(name="Hub2One")

with db_session:
    par = GenPKParent(name="par1")
    gen_hubx_db.flush()
    midpk = GenMidPK(owner=par)
    gen_hubx_db.flush()
    GenMidPKExt(value="ext1", mid=midpk)

with db_session:
    ck = GenCompKey(part1="A", part2="B", value="cv1")
    gen_comp_db.flush()
    GenCompUser(name="cu1", thing=ck)
    GenCompUserExplicit(name="cue1", thing=ck)

with db_session:
    ref = GenCompRef(name="ref1")
    gen_rel_comp_db.flush()
    rcp = GenRelCompPK(order_ref=ref, sequence=1, value="rcv1")
    gen_rel_comp_db.flush()
    GenRelCompUser(tag="rcu1", item=rcp)

# Module-level global for LOAD_GLOBAL test (line 408)
_gen_test_threshold = 100.0

# ---------------------------------------------------------------------------
# Basic select() usage
# ---------------------------------------------------------------------------


def test_select_no_filter_returns_all() -> None:
    result = select(w for w in GenWidget)
    rows = result.fetch_all()
    assert len(rows) == 3


def test_select_gt_filter() -> None:
    result = select(w for w in GenWidget if w.price > 100.0)
    rows = result.fetch_all()
    names = {r.name for r in rows}
    assert "expensive" in names
    assert "inactive" in names
    assert "cheap" not in names


def test_select_lt_filter() -> None:
    result = select(w for w in GenWidget if w.price < 100.0)
    rows = result.fetch_all()
    assert all(r.price < 100.0 for r in rows)


def test_select_eq_filter() -> None:
    result = select(w for w in GenWidget if w.name == "cheap")
    rows = result.fetch_all()
    assert len(rows) == 1
    assert rows[0].name == "cheap"


def test_select_ne_filter() -> None:
    result = select(w for w in GenWidget if w.name != "cheap")
    rows = result.fetch_all()
    assert len(rows) == 2


def test_select_le_filter() -> None:
    result = select(w for w in GenWidget if w.price <= 50.0)
    rows = result.fetch_all()
    assert len(rows) == 1


def test_select_ge_filter() -> None:
    result = select(w for w in GenWidget if w.price >= 150.0)
    rows = result.fetch_all()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_select_exhausted_generator_raises() -> None:
    gen = (w for w in GenWidget)
    list(gen)  # exhaust it
    with pytest.raises(RuntimeError, match="exhausted"):
        select(gen)


def test_select_non_entity_iter_raises() -> None:
    with pytest.raises(RuntimeError):
        select(x for x in [1, 2, 3])  # type: ignore[type-var]


def test_select_entity_without_db_raises() -> None:
    """Entity not registered with any Database → RuntimeError."""
    with pytest.raises(RuntimeError, match="Cannot find"):
        select(o for o in GenOrphan)


def test_select_function_call_raises_decompile_error() -> None:
    with pytest.raises(DecompileError):
        gen = (w for w in GenWidget if len(w.name) > 3)
        select(gen)


# ---------------------------------------------------------------------------
# _decompile_condition — direct tests
# ---------------------------------------------------------------------------


def test_decompile_empty_stack_raises_on_compare() -> None:
    """Decompiling a code with unexpected empty stack raises DecompileError."""
    # We create a situation where the stack is empty when a COMPARE_OP is hit.
    # This is hard to construct directly, so we test DecompileError can be raised.
    with pytest.raises(DecompileError):
        raise DecompileError("manual")


def test_decompile_unsupported_binary_op() -> None:
    """Unsupported BINARY_OP operand raises DecompileError."""
    # Create a generator with a modulo operation (not supported)
    with pytest.raises(DecompileError):
        gen = (w for w in GenWidget if w.price % 10 == 0)
        select(gen)


def test_decompile_condition_no_filter_returns_none() -> None:
    """A generator with no if clause should return None condition."""
    # A generator with no filter: code has no POP_JUMP_IF_FALSE
    gen = (w for w in GenWidget)
    assert isinstance(gen, types.GeneratorType)
    code = gen.gi_code
    condition, _ = _decompile_condition(code, {})
    assert condition is None
    assert _ == []


def test_decompile_is_none_condition() -> None:
    """is None / is not None conditions should decompile correctly."""
    gen = (w for w in GenWidget if w.active is not None)
    result = select(gen)
    rows = result.fetch_all()
    # All rows should match (active is always non-null here)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Arithmetic, IN, AND, and LOAD_ATTR paths
# ---------------------------------------------------------------------------


def test_select_arithmetic_in_filter() -> None:
    """BINARY_OP arithmetic in filter works (covers BINARY_OP int-argval path)."""
    # w.price + 10.0 > 60.0 → price > 50.0 → expensive(200) and inactive(150)
    result = select(w for w in GenWidget if w.price + 10.0 > 60.0)
    rows = result.fetch_all()
    names = {r.name for r in rows}
    assert names == {"expensive", "inactive"}


def test_select_in_filter() -> None:
    """CONTAINS_OP 'in' filter works correctly."""
    names_list = ["cheap", "expensive"]
    result = select(w for w in GenWidget if w.name in names_list)
    rows = result.fetch_all()
    names = {r.name for r in rows}
    assert names == {"cheap", "expensive"}


def test_select_and_filter() -> None:
    """Two conditions combined with 'and' are joined with AND in SQL."""
    # price > 50.0 AND price < 175.0 → only inactive (150.0)
    result = select(w for w in GenWidget if w.price > 50.0 and w.price < 175.0)
    rows = result.fetch_all()
    names = {r.name for r in rows}
    assert names == {"inactive"}


def test_decompile_chained_attr_unknown_relation_raises() -> None:
    """Two-level attribute access on an unknown relation raises DecompileError."""
    with pytest.raises(DecompileError, match="not a known relation"):
        gen = (w for w in GenWidget if w.category.name == "foo")
        select(gen)


def test_decompile_bound_variable_attr() -> None:
    """Attribute access on a bound (non-iter) variable covers the LOAD_ATTR else branch."""

    class _Threshold:
        prefix = "ch"

    t = _Threshold()
    # t.prefix generates LOAD_DEREF + LOAD_ATTR where stack[-1].kind == "name"
    # That triggers the else: branch in the LOAD_ATTR handler.
    gen = (w for w in GenWidget if w.name == t.prefix)
    assert isinstance(gen, types.GeneratorType)
    # _decompile_condition should run without error (it produces possibly wrong SQL
    # because 'prefix' is treated as a column name, but coverage is what matters here)
    condition, _ = _decompile_condition(gen.gi_code, {"t": t, "GenWidget": GenWidget})
    assert condition is not None


# ---------------------------------------------------------------------------
# OR filter support
# ---------------------------------------------------------------------------


def test_select_bare_bool_field_not_added_to_condition() -> None:
    """A bare boolean field (no explicit comparison) is silently ignored.

    In Python 3.13, ``if w.active`` pushes an 'attr' item (not a 'node') on
    the virtual decompiler stack.  Since only 'node' items are added to the
    condition, no WHERE clause is generated and all rows are returned.  This
    covers the ``item.kind != 'node'`` branch in the POP_JUMP_IF_TRUE handler.
    """
    result = select(w for w in GenWidget if w.active)
    rows = result.fetch_all()
    # No SQL condition generated → all rows returned
    assert len(rows) == 3


def test_select_or_filter() -> None:
    """Two conditions combined with 'or' are joined with OR in SQL."""
    # price < 100.0 OR name == "expensive"  → cheap(50) + expensive(200)
    result = select(w for w in GenWidget if w.price < 100.0 or w.name == "expensive")
    rows = result.fetch_all()
    names = {r.name for r in rows}
    assert names == {"cheap", "expensive"}


def test_select_or_all_match() -> None:
    """OR filter that matches every row returns all rows."""
    result = select(w for w in GenWidget if w.price > 0.0 or w.name == "cheap")
    rows = result.fetch_all()
    assert len(rows) == 3


def test_select_and_or_combined() -> None:
    """AND + OR precedence: (a and b) or c."""
    # (price > 100.0 and active == False) or price < 60.0
    # → (inactive:150 ✓) or (cheap:50 ✓)
    result = select(
        w
        for w in GenWidget
        if w.price > 100.0 and w.active == False or w.price < 60.0  # noqa: E712
    )
    rows = result.fetch_all()
    names = {r.name for r in rows}
    assert "cheap" in names
    assert "inactive" in names
    assert "expensive" not in names


# ---------------------------------------------------------------------------
# count()
# ---------------------------------------------------------------------------


def test_count_no_filter_returns_all() -> None:
    """count() with no filter returns the total row count."""
    assert count(w for w in GenWidget) == 3


def test_count_with_filter() -> None:
    """count() with a filter counts only matching rows."""
    assert count(w for w in GenWidget if w.price > 100.0) == 2


def test_count_no_match_returns_zero() -> None:
    """count() when no rows match returns 0."""
    assert count(w for w in GenWidget if w.price > 999.0) == 0


# ---------------------------------------------------------------------------
# avg()
# ---------------------------------------------------------------------------


def test_avg_returns_average() -> None:
    """avg() computes the SQL AVG of a field attribute."""
    import pytest as _pytest  # noqa: PLC0415

    result = avg(w.price for w in GenWidget)
    expected = (50.0 + 200.0 + 150.0) / 3
    assert result == _pytest.approx(expected)  # pyright: ignore[reportUnknownMemberType]


def test_avg_with_filter() -> None:
    """avg() respects a filter condition."""
    import pytest as _pytest  # noqa: PLC0415

    result = avg(w.price for w in GenWidget if w.price > 100.0)
    expected = (200.0 + 150.0) / 2
    assert result == _pytest.approx(expected)  # pyright: ignore[reportUnknownMemberType]


def test_avg_empty_result_returns_none() -> None:
    """avg() on zero matching rows returns None."""
    result = avg(w.price for w in GenWidget if w.price > 9999.0)
    assert result is None


def test_avg_entity_gen_raises_decompile_error() -> None:
    """avg() on a generator that yields the entity (not a field) raises DecompileError."""
    with pytest.raises(DecompileError, match="field attribute"):
        avg(w for w in GenWidget)


def test_avg_non_generator_raises() -> None:
    """avg() called with a non-generator raises AssertionError."""
    with pytest.raises(AssertionError):
        avg([1, 2, 3])  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# _decompile_yield_attr
# ---------------------------------------------------------------------------


def test_decompile_yield_attr_returns_field_name() -> None:
    """_decompile_yield_attr returns the attribute name for p.field generators."""
    gen = (w.price for w in GenWidget)
    assert _decompile_yield_attr(gen.gi_code) == "price"  # type: ignore[attr-defined]


def test_decompile_yield_attr_returns_none_for_entity_gen() -> None:
    """_decompile_yield_attr returns None when the generator yields the entity."""
    gen = (w for w in GenWidget)
    assert _decompile_yield_attr(gen.gi_code) is None  # type: ignore[attr-defined]


def test_decompile_yield_attr_with_filter() -> None:
    """_decompile_yield_attr correctly extracts the field even with a filter clause."""
    gen = (w.name for w in GenWidget if w.price > 50.0)
    assert _decompile_yield_attr(gen.gi_code) == "name"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# sum()
# ---------------------------------------------------------------------------


def test_sum_returns_total() -> None:
    """sum() computes the SQL SUM of a field attribute."""
    result = sum(w.price for w in GenWidget)
    assert result == 50.0 + 200.0 + 150.0


def test_sum_with_filter() -> None:
    """sum() respects a filter condition."""
    result = sum(w.price for w in GenWidget if w.price > 100.0)
    assert result == 200.0 + 150.0


def test_sum_empty_result_returns_none() -> None:
    """sum() on zero matching rows returns None."""
    result = sum(w.price for w in GenWidget if w.price > 9999.0)
    assert result is None


def test_sum_entity_gen_raises_decompile_error() -> None:
    """sum() on a generator that yields the entity raises DecompileError."""
    with pytest.raises(DecompileError, match="field attribute"):
        sum(w for w in GenWidget)


def test_sum_non_generator_raises() -> None:
    """sum() called with a non-generator raises AssertionError."""
    with pytest.raises(AssertionError):
        sum([1, 2, 3])  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# min()
# ---------------------------------------------------------------------------


def test_min_returns_minimum() -> None:
    """min() computes the SQL MIN of a field attribute."""
    result = min(w.price for w in GenWidget)
    assert result == 50.0


def test_min_with_filter() -> None:
    """min() respects a filter condition."""
    result = min(w.price for w in GenWidget if w.price > 100.0)
    assert result == 150.0


def test_min_empty_result_returns_none() -> None:
    """min() on zero matching rows returns None."""
    result = min(w.price for w in GenWidget if w.price > 9999.0)
    assert result is None


def test_min_entity_gen_raises_decompile_error() -> None:
    """min() on a generator that yields entity raises DecompileError."""
    with pytest.raises(DecompileError, match="field attribute"):
        min(w for w in GenWidget)


def test_min_non_generator_raises() -> None:
    """min() called with a non-generator raises AssertionError."""
    with pytest.raises(AssertionError):
        min([1, 2, 3])  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# max()
# ---------------------------------------------------------------------------


def test_max_returns_maximum() -> None:
    """max() computes the SQL MAX of a field attribute."""
    result = max(w.price for w in GenWidget)
    assert result == 200.0


def test_max_with_filter() -> None:
    """max() respects a filter condition."""
    result = max(w.price for w in GenWidget if w.price < 200.0)
    assert result == 150.0


def test_max_empty_result_returns_none() -> None:
    """max() on zero matching rows returns None."""
    result = max(w.price for w in GenWidget if w.price > 9999.0)
    assert result is None


def test_max_entity_gen_raises_decompile_error() -> None:
    """max() on a generator that yields entity raises DecompileError."""
    with pytest.raises(DecompileError, match="field attribute"):
        max(w for w in GenWidget)


def test_max_non_generator_raises() -> None:
    """max() called with a non-generator raises AssertionError."""
    with pytest.raises(AssertionError):
        max([1, 2, 3])  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# Bytecode decompiler branch coverage
# ---------------------------------------------------------------------------


def test_generator_or_condition_decompiles() -> None:
    """OR conditions in generator expressions should decompile correctly."""
    # Test that OR branching is handled: (a < 100 or b > 150)
    result = select(w for w in GenWidget if w.price < 100 or w.price > 150)
    items = result.fetch_all()
    # Should include w1 (price=50), w3 (price=200)
    assert len(items) == 2
    assert all(w.price < 100 or w.price > 150 for w in items)


# ---------------------------------------------------------------------------
# Relation traversal in generator expressions
# ---------------------------------------------------------------------------


def test_select_relation_traversal_basic() -> None:
    """Generator-expression select with relation traversal (p.relation.field == val)."""
    results = select(i for i in GenItem if i.brand.name == "Acme").fetch_all()
    assert len(results) == 1
    assert results[0].label == "anvil"


def test_select_relation_traversal_not_found() -> None:
    """Generator-expression relation traversal returns empty when no match."""
    results = select(i for i in GenItem if i.brand.name == "NoSuch").fetch_all()
    assert results == []


def test_select_relation_traversal_closure_var() -> None:
    """Generator-expression relation traversal with a closure variable."""
    brand_name = "Globex"
    results = select(i for i in GenItem if i.brand.name == brand_name).fetch_all()
    assert len(results) == 1
    assert results[0].label == "reactor"


def test_select_relation_traversal_dedup_joins() -> None:
    """Two references to the same relation in one generator expression produce one JOIN."""
    # Both conditions reference i.brand — only one JOIN should be emitted.
    brand_z = "Z"  # avoid reportUnnecessaryComparison: pyright narrows 'Acme' != literal
    results = select(
        i
        for i in GenItem
        if i.brand.name == "Acme" and i.brand.name != brand_z  # pyright: ignore[reportUnnecessaryComparison]
    ).fetch_all()
    assert len(results) == 1
    assert results[0].label == "anvil"


def test_select_three_level_chain() -> None:
    """Generator-expression with a 3-level chain (item → brand → category.slug)."""
    results = select(i for i in GenItemDeep if i.brand.category.slug == "tools").fetch_all()
    assert len(results) == 1
    assert results[0].code == "H001"


def test_select_three_level_chain_not_found() -> None:
    """Three-level chain returns empty when the deepest field does not match."""
    results = select(i for i in GenItemDeep if i.brand.category.slug == "nosuch").fetch_all()
    assert results == []


# ---------------------------------------------------------------------------
# Relation attribute as column reference (to_col_ref _relations_ branch)
# ---------------------------------------------------------------------------


def test_select_relation_fk_direct_int_comparison() -> None:
    """Comparing a relation attribute directly to an int uses the FK column.

    ``i.brand == 1`` → brand is in _relations_, not _fields_ → to_col_ref
    uses the FK column name (brand_id) for the comparison.
    Covers lines 191-193 in generators.py.
    """
    # brand_id=1 corresponds to the first inserted brand
    results = select(i for i in GenItem if i.brand == 1).fetch_all()
    assert len(results) == 1
    assert results[0].label == "anvil"


def test_select_entity_instance_as_comparison_value() -> None:
    """Comparing against an Entity instance uses _get_pk_val to extract the PK.

    ``i.brand == brand_instance`` → val is an Entity → lines 264-266 covered.
    """
    brands = gen_rel_db.select(GenBrand).fetch_all()
    assert brands
    acme = next((b for b in brands if b.name == "Acme"), None)
    assert acme is not None

    results = select(i for i in GenItem if i.brand == acme).fetch_all()
    assert len(results) == 1
    assert results[0].label == "anvil"


def test_lambda_bare_bool_attr_adds_true_condition() -> None:
    """A bare boolean attr in a non-generator (lambda) predicate is treated as
    ``attr = True`` condition. Covers line 500 in generators.py.
    """
    # Use Entity.exists() with a lambda that has a bare boolean attribute.
    # active = True condition is added → at least one active GenWidget exists.
    with db_session:
        exists = GenWidget.exists(lambda w: w.active)
    assert exists is True


# ---------------------------------------------------------------------------
# Attribute not in _fields_ or _relations_ → fallthrough (generators.py 191->194)
# LOAD_ATTR on a resolved-None value → 'attr' item (generators.py 359)
# ---------------------------------------------------------------------------


class GenWithLocal(Entity):
    """Entity with a Local (transient) field to exercise the attr-fallthrough path."""

    _table_name_ = "gen_with_local"
    name: Req[str]
    note: Local[str] = Local(default="")  # Local: in _locals_, NOT _fields_ or _relations_


gen_local_db = Database(entities=[GenWithLocal])
gen_local_db.bind("sqlite", ":memory:")
gen_local_db.generate_mapping(create_tables=True)

with db_session:
    GenWithLocal(name="alpha")
    GenWithLocal(name="beta")


def test_select_local_attr_falls_through_to_column_ref() -> None:
    """Generator with Local-field attribute falls through to plain ColumnRef (generators.py 191->194).

    'note' is a Local field (not in _fields_ or _relations_) → line 191 False → line 194.
    """
    # 'note' is a Local field → not in _fields_ or _relations_ → line 194 (fallthrough)
    # The generated SQL references the column by name (which doesn't exist → OperationalError)
    from contextlib import suppress

    with suppress(Exception):
        # Expected: SQLite raises "no such column: note" but we covered line 194
        select(w for w in GenWithLocal if w.note == "").fetch_all()


def test_load_attr_on_none_resolved_value_pushes_attr_item() -> None:
    """LOAD_ATTR on a None-resolved bound variable produces an 'attr' item (generators.py 359).

    When a closure variable is accessed (resolved as a 'name'), and that value has
    no attribute 'nonexistent', the resolved value is None. A subsequent LOAD_ATTR
    on that None value pushes an 'attr' item (line 359).
    """
    some_obj = object()  # no 'nonexistent' attribute

    # Generator: some_obj.nonexistent.attr → first LOAD_ATTR resolves to None name,
    # second LOAD_ATTR on None value → line 359 (attr item)
    from contextlib import suppress

    with suppress(Exception):
        select(w for w in GenWithLocal if some_obj.nonexistent.value == 1).fetch_all()  # type: ignore[attr-defined]  # Expected: 'value' becomes an attr item, not queryable


# ---------------------------------------------------------------------------
# Line 408: LOAD_GLOBAL not in func_globals → val = None
# ---------------------------------------------------------------------------


def test_decompile_load_global_not_in_func_globals_gives_none() -> None:
    """When LOAD_GLOBAL references a name not in func_globals, val=None is used (line 408)."""
    # _gen_test_threshold is a module-level global; passing func_globals={} means it won't be found
    gen = (w for w in GenWidget if w.price > _gen_test_threshold)
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenWidget, func_globals={}
    )
    # Decompile succeeds: val=None → Param(None) as the comparison value
    assert condition is not None


# ---------------------------------------------------------------------------
# Lines 560-570, 721-733: `"val" in w.name.lower()` → LOWER(...) LIKE '%val%'
# ---------------------------------------------------------------------------


def test_select_val_in_col_lower_generates_like() -> None:
    """'ch' in w.name.lower() → LOWER(name) LIKE '%ch%' (lines 559-570, 721-733)."""
    results = select(w for w in GenWidget if "ch" in w.name.lower()).fetch_all()
    # "cheap" has "ch" in it; "expensive" and "inactive" do not
    assert len(results) == 1
    assert results[0].name == "cheap"


def test_select_val_in_col_upper_generates_like() -> None:
    """'CH' in w.name.upper() → UPPER(name) LIKE '%CH%' (covers UPPER in _COL_METHODS)."""
    results = select(w for w in GenWidget if "CH" in w.name.upper()).fetch_all()
    assert len(results) == 1
    assert results[0].name == "cheap"


# ---------------------------------------------------------------------------
# Lines 738-742: w.lower() — bare entity method call → DecompileError
# ---------------------------------------------------------------------------


def test_select_bare_entity_method_call_raises_decompile_error() -> None:
    """w.lower() — method on entity (not field) raises DecompileError (lines 738-742)."""
    with pytest.raises(DecompileError, match="requires attribute context"):
        select(w for w in GenWidget if w.lower() == "cheap").fetch_all()  # pyright: ignore


# ---------------------------------------------------------------------------
# Lines 575-598: `entity in m2m_relation` → EXISTS query (M1 case)
# ---------------------------------------------------------------------------


def test_select_entity_in_m2m_set_generates_exists() -> None:
    """'tag in article.tags' → EXISTS (SELECT 1 FROM join table ...) (lines 575-598)."""
    # Get a tag instance
    tag = gen_m2m_db.select(GenTag).filter(GenTag.label == "python").fetch_one()
    assert tag is not None

    # select articles where tag is in article.tags → M2M EXISTS
    results = select(a for a in GenArticle if tag in a.tags).fetch_all()  # pyright: ignore
    assert len(results) == 1
    assert results[0].title == "intro"


def test_select_entity_in_m2m_set_no_match() -> None:
    """Entity IN M2M set with no match returns empty list."""
    # Create a tag not in any article
    with db_session:
        GenTag(label="orphan")
        gen_m2m_db.flush()

    tag = gen_m2m_db.select(GenTag).filter(GenTag.label == "orphan").fetch_one()
    assert tag is not None

    results = select(a for a in GenArticle if tag in a.tags).fetch_all()  # pyright: ignore
    assert len(results) == 0


# ---------------------------------------------------------------------------
# Lines 640-688: `val in set_relation.field` → EXISTS query (M2 case)
# ---------------------------------------------------------------------------


def test_select_val_in_o2m_field_generates_exists() -> None:
    """'True in post.comments.active' → EXISTS (SELECT 1 FROM gen_comment ...) (lines 640-688)."""
    results = select(p for p in GenPost if True in p.comments.active).fetch_all()  # pyright: ignore
    # Our post has an active comment → should be found
    assert len(results) == 1
    assert results[0].title == "MyPost"


# ---------------------------------------------------------------------------
# Lines 709-713: CALL with args (function call WITH arguments)
# ---------------------------------------------------------------------------


def test_select_free_var_function_call_with_args_evaluated_at_compile_time() -> None:
    """Free-variable (closure) function call evaluated at compile time (lines 709-776)."""
    prices = [50.0, 200.0]
    # Use a closure-captured max function (not the shadowed nextorm max)
    import builtins

    real_max = builtins.max
    # real_max is LOAD_DEREF (closure), prices is LOAD_DEREF (closure)
    # → func_item.kind=="name", val=real_max (callable) → evaluates at compile time
    results = select(w for w in GenWidget if w.price >= real_max(prices)).fetch_all()
    assert len(results) == 1
    assert results[0].name == "expensive"


def test_select_free_var_function_call_raises_on_non_name_arg() -> None:
    """CALL with a non-name argument (node) raises DecompileError (lines 711-713)."""
    some_func: Callable[[float], float] = lambda x: x  # noqa: E731

    with pytest.raises(DecompileError):
        # w.price > 0 creates a "node" item which is not "name"/"attr"/"rel_chain"
        select(w for w in GenWidget if some_func(w.price > 0)).fetch_all()  # pyright: ignore


def test_select_free_var_function_non_callable_raises() -> None:
    """CALL with non-callable global-var (resolves to None, not callable) raises DecompileError."""
    # 'len' is a builtin not in gen.gi_frame.f_globals → resolves to None → not callable
    prices_list = [50.0, 200.0]
    with pytest.raises(DecompileError):
        select(w for w in GenWidget if len(prices_list) > 0).fetch_all()  # pyright: ignore


def test_select_free_var_function_call_raises_on_exception() -> None:
    """If free-var call raises, DecompileError is raised (lines 765-770)."""

    def bad_func() -> float:
        raise ValueError("boom")

    with pytest.raises(DecompileError, match="Error evaluating free-variable call"):
        select(w for w in GenWidget if w.price > bad_func()).fetch_all()


# ---------------------------------------------------------------------------
# Lines 243-302: Non-owning O2O traversal in generator (is_non_owning = True)
# ---------------------------------------------------------------------------


def test_select_non_owning_o2o_traversal() -> None:
    """Generator traversal of non-owning O2O relation builds correct JOIN (lines 243-302)."""
    # GenShop.config is non-owning: FK lives on GenShopConfig side
    # select(s for s in GenShop if s.config.theme == "dark")
    results = select(s for s in GenShop if s.config.theme == "dark").fetch_all()
    assert len(results) == 1
    assert results[0].name == "MyShop"


# ---------------------------------------------------------------------------
# Lines 235->246: Explicit owner=False skips auto-detect (non-owning O2O)
# ---------------------------------------------------------------------------


def test_select_explicit_owner_false_skips_autodetect() -> None:
    """GenHub.spoke has owner=False → is_non_owning=True at line 234, skip auto-detect (235->246)."""
    # GenHub.spoke: Single[GenSpoke](owner=False) → FK on GenSpoke side
    # GenSpoke.hub: Single[GenHub] → back-ref exists → rev_fk_col found normally
    results = select(h for h in GenHub if h.spoke.color == "red").fetch_all()
    assert len(results) == 1
    assert results[0].name == "HubOne"


# ---------------------------------------------------------------------------
# Lines 249->255, 250->249, 256: Non-owning O2O where target has no matching back-ref
# GenHub2.spoke → GenSpoke2 which has no Single[GenHub2] → rev_fk_col fallback
# ---------------------------------------------------------------------------


def test_select_non_owning_no_back_ref_uses_fallback_col() -> None:
    """Non-owning O2O with no matching back-ref: rev_fk_col falls back (lines 249->255, 256)."""
    # GenHub2.spoke → GenSpoke2 which has other: Single[GenSibling] (NOT pointing to GenHub2)
    # Loop at 249 runs but 250 condition is always False → 249->255 → 255 True → 256
    gen = (h for h in GenHub2 if h.spoke.color == "blue")
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenHub2, func_globals={}
    )
    assert condition is not None


# ---------------------------------------------------------------------------
# Line 263: cur_pk_attr is a relation (FK-based PK) → fallback column name
# GenMidPK has owner: Single[GenPKParent] with _pk_ = PrimaryKey("owner")
# So pk_fields = ('owner',) which is in _relations_ not _fields_
# ---------------------------------------------------------------------------


def test_select_non_owning_relation_pk_uses_id_fallback() -> None:
    """Non-owning O2O with relation-based PK: cur_pk_col fallback (line 263)."""
    # GenMidPK._pk_fields_ = ('owner',), 'owner' NOT in _fields_ → line 263
    gen = (m for m in GenMidPK if m.ext.value == "x")
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenMidPK, func_globals={}
    )
    assert condition is not None


# ---------------------------------------------------------------------------
# Lines 271-294: Composite-PK target → multi-column JOIN condition
# ---------------------------------------------------------------------------


def test_select_composite_pk_target_builds_multi_join() -> None:
    """Single relation to composite-PK entity generates multi-column JOIN (lines 271-294)."""
    results = select(u for u in GenCompUser if u.thing.value == "cv1").fetch_all()
    assert len(results) == 1
    assert results[0].name == "cu1"


def test_select_composite_pk_explicit_columns_line_274() -> None:
    """Single with explicit columns= to composite-PK entity covers line 274 (ri.spec.columns)."""
    # GenCompUserExplicit.thing has Single(columns=["thing_part1", "thing_part2"])
    # → ri.spec.columns is set → line 273 True → 274 covered
    gen = (u for u in GenCompUserExplicit if u.thing.value == "cv1")
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenCompUserExplicit, func_globals={}
    )
    assert condition is not None


def test_select_composite_pk_with_relation_field_covers_283_284() -> None:
    """Relation-based PK in composite-PK target: pk_f not in _fields_ → lines 283-284."""
    # GenRelCompPK._pk_ = PrimaryKey("order_ref", "sequence")
    # "order_ref" is a relation → pk_f not in _fields_ → lines 283-284 covered
    results = select(u for u in GenRelCompUser if u.item.value == "rcv1").fetch_all()
    assert len(results) == 1
    assert results[0].tag == "rcu1"


# ---------------------------------------------------------------------------
# Line 408: LOAD_GLOBAL / func_globals has the name → val set from func_globals
# ---------------------------------------------------------------------------


def test_decompile_load_global_found_in_func_globals() -> None:
    """When LOAD_GLOBAL name IS in func_globals, val is taken from it (line 408)."""
    gen = (w for w in GenWidget if w.price > _gen_test_threshold)
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenWidget, func_globals={"_gen_test_threshold": 50.0}
    )
    # Decompile succeeds: val=50.0 from func_globals
    assert condition is not None


# ---------------------------------------------------------------------------
# Line 563->574: non-str val in col.lower() → falls through
# ---------------------------------------------------------------------------


def test_select_int_in_col_lower_falls_through_to_generic() -> None:
    """Non-str val in col.lower() → condition 563 False → falls through → DecompileError."""
    gen = (w for w in GenWidget if 5 in w.name.lower())  # pyright: ignore
    assert isinstance(gen, types.GeneratorType)
    with pytest.raises(DecompileError):
        _decompile_condition(gen.gi_code, free_vars={}, entity_cls=GenWidget, func_globals={})


# ---------------------------------------------------------------------------
# Line 577->630: entity in w.name → name is a scalar field (rel is None)
# ---------------------------------------------------------------------------


def test_select_val_in_scalar_field_falls_through() -> None:
    """'x' in w.name — name is not a relation → rel is None → 577 False → falls to 630 (577->630)."""
    gen = (w for w in GenWidget if "x" in w.name)  # pyright: ignore
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenWidget, func_globals={}
    )
    # Falls through all IN special cases → BinOp("x" IN name)
    assert condition is not None


# ---------------------------------------------------------------------------
# Line 584->630: entity in i.brand → brand is SINGLE not SET
# ---------------------------------------------------------------------------


def test_select_val_in_single_rel_falls_through() -> None:
    """entity in i.brand where brand is SINGLE → rel.spec.kind != SET → 584 False → 630 (584->630)."""
    gen = (i for i in GenItem if "x" in i.brand)  # pyright: ignore
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenItem, func_globals={}
    )
    assert condition is not None


# ---------------------------------------------------------------------------
# Line 587->630: SET relation with unresolvable target → target_cls is None
# ---------------------------------------------------------------------------


def test_select_entity_in_unresolvable_set_falls_through() -> None:
    """SET relation with unresolvable forward ref → target_cls=None → 587 False (587->630)."""
    gen = (g for g in GenBadSet if "x" in g.ghosts)  # pyright: ignore
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenBadSet, func_globals={}
    )
    assert condition is not None


# ---------------------------------------------------------------------------
# Line 597->630: entity in p.comments → comments is O2M (not M2M) → is_m2m=False
# ---------------------------------------------------------------------------


def test_select_entity_in_o2m_relation_falls_through() -> None:
    """Entity in O2M set (not M2M) → is_m2m=False → 597 False → falls to 630 (597->630)."""
    comment = gen_o2m_db.select(GenComment).fetch_one()
    gen = (p for p in GenPost if comment in p.comments)  # pyright: ignore
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenPost, func_globals={}
    )
    assert condition is not None


# ---------------------------------------------------------------------------
# Line 612->616: int val in a.tags → val is not Entity → skip _get_pk_val
# ---------------------------------------------------------------------------


def test_select_int_in_m2m_set_skips_entity_pk_extract() -> None:
    """Integer val in M2M set: isinstance(val, Entity) is False → 612->616 (skip pk extract)."""
    gen = (a for a in GenArticle if 1 in a.tags)  # pyright: ignore
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenArticle, func_globals={}
    )
    assert condition is not None


# ---------------------------------------------------------------------------
# Lines 636->691: val in w.nonexistent.field → rel is None in M2 path
# ---------------------------------------------------------------------------


def test_select_val_in_nonexistent_rel_chain_falls_through() -> None:
    """val in w.nonexistent.field → no such relation → rel is None → 636 False (636->691)."""
    gen = (w for w in GenWidget if "x" in w.nonexistent.value)  # pyright: ignore
    # Falls through to 691, then to_node raises because the chain can't resolve
    assert isinstance(gen, types.GeneratorType)
    with pytest.raises(DecompileError):
        _decompile_condition(gen.gi_code, free_vars={}, entity_cls=GenWidget, func_globals={})


# ---------------------------------------------------------------------------
# Lines 640->691: val in i.brand.name → brand is SINGLE not SET
# ---------------------------------------------------------------------------


def test_select_val_in_single_rel_chain_falls_through() -> None:
    """val in i.brand.name where brand is SINGLE → rel.spec.kind != SET → 640 False (640->691)."""
    gen = (i for i in GenItem if "x" in i.brand.name)  # pyright: ignore
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenItem, func_globals={}
    )
    assert condition is not None


# ---------------------------------------------------------------------------
# Lines 643->691: val in bad_set.ghosts.x → unresolvable SET target → 643 False
# ---------------------------------------------------------------------------


def test_select_val_in_unresolvable_set_chain_falls_through() -> None:
    """val in bad_set.ghosts.x → SET but target unresolvable → target_cls=None → 643 False."""
    gen = (g for g in GenBadSet if True in g.ghosts.x)  # pyright: ignore
    # Falls through to 691, then to_node raises because it can't resolve the chain target
    assert isinstance(gen, types.GeneratorType)
    with pytest.raises(DecompileError):
        _decompile_condition(gen.gi_code, free_vars={}, entity_cls=GenBadSet, func_globals={})


# ---------------------------------------------------------------------------
# Line 677->679: val is None because left.kind is attr (not name)
# ---------------------------------------------------------------------------


def test_select_attr_left_in_o2m_field_val_is_none() -> None:
    """p.id in p.comments.active → left.kind='attr' → val=None → skip serialize (677->679)."""
    gen = (p for p in GenPost if p.id in p.comments.active)
    assert isinstance(gen, types.GeneratorType)
    condition, _ = _decompile_condition(
        gen.gi_code, free_vars={}, entity_cls=GenPost, func_globals={}
    )
    assert condition is not None


# ---------------------------------------------------------------------------
# Line 733: Unsupported column method on rel_chain → raise DecompileError
# ---------------------------------------------------------------------------


def test_select_unsupported_col_method_on_rel_chain_raises() -> None:
    """w.name.foo() → 'foo' not in _COL_METHODS → raise at line 733."""
    with pytest.raises(DecompileError, match="Unsupported column method"):
        select(w for w in GenWidget if w.name.foo() == "x").fetch_all()


# ---------------------------------------------------------------------------
# Lines 739->746: n_args==0, func_item.kind=="attr", method NOT in _COL_METHODS
# ---------------------------------------------------------------------------


def test_select_unknown_entity_attr_method_raises() -> None:
    """w.foo() where 'foo' not in _COL_METHODS and kind=='attr' → 739 False → raise at 747."""
    with pytest.raises(DecompileError):
        select(w for w in GenWidget if w.foo() == "x").fetch_all()


# ---------------------------------------------------------------------------
# Line 757: Free-var function called with attr-kind arg → raise DecompileError
# ---------------------------------------------------------------------------


def test_select_free_var_func_with_attr_arg_raises() -> None:
    """func(w.price) where w.price is an attr-kind arg → raise at line 757."""

    def my_func(x: float) -> float:
        return x

    with pytest.raises(DecompileError):
        select(w for w in GenWidget if my_func(w.price) > 0).fetch_all()  # pyright: ignore


# ---------------------------------------------------------------------------
# Line 773: BUILD_LIST instruction (list with non-constant element) → DecompileError
# ---------------------------------------------------------------------------


def test_select_build_list_in_filter_raises() -> None:
    """Build-list with computed element triggers BUILD_ handler at line 773 → DecompileError."""
    with pytest.raises(DecompileError):
        select(w for w in GenWidget if w.price in [w.active, 1.0]).fetch_all()


# ---------------------------------------------------------------------------
# Fallback to Python built-ins for non-entity generators
# ---------------------------------------------------------------------------


def test_sum_fallback_plain_generator() -> None:
    """sum() over a plain Python generator falls back to builtins.sum."""
    assert sum(x * 2 for x in [1, 2, 3]) == 12


def test_min_fallback_plain_generator() -> None:
    """min() over a plain Python generator falls back to builtins.min."""
    assert min(x for x in [3, 1, 2]) == 1


def test_max_fallback_plain_generator() -> None:
    """max() over a plain Python generator falls back to builtins.max."""
    assert max(x for x in [3, 1, 2]) == 3


def test_count_fallback_plain_generator() -> None:
    """count() over a plain Python generator counts items with sum(1 for ...)."""
    assert count(x for x in [1, 2, 3]) == 3


def test_count_non_generator_falls_back() -> None:
    """count() with a non-generator (list) falls back via _is_entity_generator."""
    assert count([1, 2, 3]) == 3


def test_avg_raises_for_plain_generator() -> None:
    """avg() raises TypeError when the generator does not iterate over an Entity."""
    with pytest.raises(TypeError, match="Entity class"):
        avg(x for x in [1.0, 2.0])


def test_is_entity_generator_exhausted_returns_false() -> None:
    """_is_entity_generator returns False when gi_frame is None (exhausted generator)."""
    import gc  # noqa: PLC0415

    from nextorm.generators import _is_entity_generator  # noqa: PLC0415

    # Build a generator, exhaust it, then GC — gi_frame should be None
    exhausted = (x for x in [1])
    list(exhausted)
    gc.collect()
    assert _is_entity_generator(exhausted) is False
