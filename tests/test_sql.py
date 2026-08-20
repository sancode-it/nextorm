"""Tests for nextorm.sql — AST nodes and SQLiteBuilder rendering."""

from __future__ import annotations

import dataclasses

import pytest

from nextorm.sql import (
    Alias,
    BinOp,
    Case,
    CaseWhen,
    Cast,
    ColumnRef,
    Delete,
    FormatBuilder,
    FunctionCall,
    Insert,
    Literal,
    OrderItem,
    Param,
    RawSql,
    Select,
    SQLiteBuilder,
    Star,
    UnaryOp,
    Update,
    render,
)
from nextorm.sql import (
    sql as sql_node,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sql(node: object) -> str:
    s, _ = render(node)  # type: ignore[arg-type]
    return s


def params(node: object) -> list[object]:
    _, p = render(node)  # type: ignore[arg-type]
    return p


# ---------------------------------------------------------------------------
# Leaf nodes
# ---------------------------------------------------------------------------


def test_param_positional() -> None:
    assert sql(Param()) == "?"


def test_param_named_still_positional_in_sqlite() -> None:
    # SQLiteBuilder ignores the name — always renders "?"
    assert sql(Param("user_id")) == "?"


def test_format_builder_param() -> None:
    """FormatBuilder renders Param nodes as %s (format param-style)."""
    builder = FormatBuilder()
    assert render(Param(), builder=builder) == ("%s", [])
    assert render(Param("x"), builder=builder) == ("%s", [])


def test_format_builder_with_value() -> None:
    """FormatBuilder collects parameter values the same as SQLiteBuilder."""
    builder = FormatBuilder()
    node = Select(
        columns=(ColumnRef("name"),),
        from_table="user",
        where=BinOp(ColumnRef("age"), ">=", Param(value=18)),
    )
    sql_str, params = render(node, builder=builder)
    assert sql_str == "SELECT name FROM user WHERE age >= %s"
    assert params == [18]


def test_literal_none() -> None:
    assert sql(Literal(None)) == "NULL"


def test_literal_true() -> None:
    assert sql(Literal(True)) == "1"


def test_literal_false() -> None:
    assert sql(Literal(False)) == "0"


def test_literal_int() -> None:
    assert sql(Literal(42)) == "42"


def test_literal_float() -> None:
    assert sql(Literal(3.14)) == "3.14"


def test_literal_string() -> None:
    assert sql(Literal("hello")) == "'hello'"


def test_literal_string_escapes_single_quote() -> None:
    assert sql(Literal("it's")) == "'it''s'"


def test_column_ref_no_table() -> None:
    assert sql(ColumnRef("name")) == "name"


def test_column_ref_with_table() -> None:
    assert sql(ColumnRef("name", "u")) == "u.name"


def test_star_bare() -> None:
    assert sql(Star()) == "*"


def test_star_with_table() -> None:
    assert sql(Star("u")) == "u.*"


# ---------------------------------------------------------------------------
# BinOp
# ---------------------------------------------------------------------------


def test_binop_equality() -> None:
    assert sql(BinOp(ColumnRef("id"), "=", Param())) == "id = ?"


def test_binop_and_parenthesises_children() -> None:
    left = BinOp(ColumnRef("a"), "=", Literal(1))
    right = BinOp(ColumnRef("b"), "=", Literal(2))
    result = sql(BinOp(left, "AND", right))
    assert result == "(a = 1) AND (b = 2)"


def test_binop_or_parenthesises_children() -> None:
    left = BinOp(ColumnRef("x"), ">", Literal(0))
    right = BinOp(ColumnRef("y"), "<", Literal(10))
    result = sql(BinOp(left, "OR", right))
    assert result == "(x > 0) OR (y < 10)"


def test_binop_no_parens_for_simple_operators() -> None:
    inner = BinOp(ColumnRef("a"), "+", Literal(1))
    result = sql(BinOp(inner, "*", Literal(2)))
    assert result == "a + 1 * 2"


def test_binop_like() -> None:
    assert sql(BinOp(ColumnRef("name"), "LIKE", Literal("%foo%"))) == "name LIKE '%foo%'"


# ---------------------------------------------------------------------------
# UnaryOp
# ---------------------------------------------------------------------------


def test_unary_not_with_simple_ref() -> None:
    assert sql(UnaryOp("NOT", ColumnRef("active"))) == "NOT active"


def test_unary_not_with_binop_adds_parens() -> None:
    inner = BinOp(ColumnRef("x"), "=", Literal(1))
    assert sql(UnaryOp("NOT", inner)) == "NOT (x = 1)"


def test_unary_minus_symbol() -> None:
    assert sql(UnaryOp("-", ColumnRef("price"))) == "-price"


def test_unary_exists() -> None:
    # EXISTS is a word operator → space before operand
    assert sql(UnaryOp("EXISTS", ColumnRef("subq"))) == "EXISTS subq"


# ---------------------------------------------------------------------------
# Cast
# ---------------------------------------------------------------------------


def test_cast() -> None:
    assert sql(Cast(ColumnRef("price"), "REAL")) == "CAST(price AS REAL)"


# ---------------------------------------------------------------------------
# FunctionCall
# ---------------------------------------------------------------------------


def test_function_count_star() -> None:
    assert sql(FunctionCall("COUNT", (Star(),))) == "COUNT(*)"


def test_function_max() -> None:
    assert sql(FunctionCall("MAX", (ColumnRef("score"),))) == "MAX(score)"


def test_function_coalesce() -> None:
    result = sql(FunctionCall("COALESCE", (ColumnRef("x"), Literal(0))))
    assert result == "COALESCE(x, 0)"


def test_function_count_distinct() -> None:
    result = sql(FunctionCall("COUNT", (ColumnRef("id"),), distinct=True))
    assert result == "COUNT(DISTINCT id)"


# ---------------------------------------------------------------------------
# Case
# ---------------------------------------------------------------------------


def test_case_searched() -> None:
    node = Case(
        whens=(CaseWhen(BinOp(ColumnRef("x"), ">", Literal(0)), Literal("pos")),),
        else_=Literal("non-pos"),
    )
    assert sql(node) == "CASE WHEN x > 0 THEN 'pos' ELSE 'non-pos' END"


def test_case_simple() -> None:
    node = Case(
        subject=ColumnRef("status"),
        whens=(
            CaseWhen(Literal("A"), Literal("active")),
            CaseWhen(Literal("I"), Literal("inactive")),
        ),
        else_=Literal("unknown"),
    )
    assert (
        sql(node) == "CASE status WHEN 'A' THEN 'active' WHEN 'I' THEN 'inactive' ELSE 'unknown' END"
    )


def test_case_no_else() -> None:
    node = Case(
        whens=(CaseWhen(BinOp(ColumnRef("n"), "=", Literal(1)), Literal("one")),),
    )
    assert sql(node) == "CASE WHEN n = 1 THEN 'one' END"


def test_case_multiple_whens() -> None:
    node = Case(
        whens=(
            CaseWhen(BinOp(ColumnRef("n"), "=", Literal(1)), Literal("one")),
            CaseWhen(BinOp(ColumnRef("n"), "=", Literal(2)), Literal("two")),
        ),
        else_=Literal("other"),
    )
    result = sql(node)
    assert "WHEN n = 1 THEN 'one'" in result
    assert "WHEN n = 2 THEN 'two'" in result
    assert result.endswith("ELSE 'other' END")


# ---------------------------------------------------------------------------
# Alias
# ---------------------------------------------------------------------------


def test_alias() -> None:
    assert sql(Alias(ColumnRef("name"), "user_name")) == "name AS user_name"


def test_alias_on_function() -> None:
    node = Alias(FunctionCall("COUNT", (Star(),)), "cnt")
    assert sql(node) == "COUNT(*) AS cnt"


# ---------------------------------------------------------------------------
# OrderItem
# ---------------------------------------------------------------------------


def test_order_asc() -> None:
    assert sql(OrderItem(ColumnRef("name"))) == "name ASC"


def test_order_desc() -> None:
    assert sql(OrderItem(ColumnRef("created_at"), descending=True)) == "created_at DESC"


def test_order_nulls_first() -> None:
    assert sql(OrderItem(ColumnRef("x"), nulls_first=True)) == "x ASC NULLS FIRST"


def test_order_nulls_last() -> None:
    assert sql(OrderItem(ColumnRef("x"), nulls_first=False)) == "x ASC NULLS LAST"


def test_order_desc_nulls_first() -> None:
    node = OrderItem(ColumnRef("x"), descending=True, nulls_first=True)
    assert sql(node) == "x DESC NULLS FIRST"


# ---------------------------------------------------------------------------
# Select
# ---------------------------------------------------------------------------


def test_select_star() -> None:
    node = Select(columns=(Star(),), from_table="user")
    assert sql(node) == "SELECT * FROM user"


def test_select_columns() -> None:
    node = Select(
        columns=(ColumnRef("id"), ColumnRef("name")),
        from_table="user",
    )
    assert sql(node) == "SELECT id, name FROM user"


def test_select_with_alias() -> None:
    node = Select(columns=(Star(),), from_table="user", from_alias="u")
    assert sql(node) == "SELECT * FROM user AS u"


def test_select_where() -> None:
    node = Select(
        columns=(Star(),),
        from_table="user",
        where=BinOp(ColumnRef("id"), "=", Param()),
    )
    assert sql(node) == "SELECT * FROM user WHERE id = ?"


def test_select_distinct() -> None:
    node = Select(
        columns=(ColumnRef("email"),),
        from_table="user",
        distinct=True,
    )
    assert sql(node) == "SELECT DISTINCT email FROM user"


def test_select_order_by() -> None:
    node = Select(
        columns=(Star(),),
        from_table="user",
        order_by=(OrderItem(ColumnRef("name"), descending=True),),
    )
    assert sql(node) == "SELECT * FROM user ORDER BY name DESC"


def test_select_limit_offset() -> None:
    node = Select(
        columns=(Star(),),
        from_table="user",
        limit=10,
        offset=20,
    )
    assert sql(node) == "SELECT * FROM user LIMIT 10 OFFSET 20"


def test_select_limit_only() -> None:
    node = Select(columns=(Star(),), from_table="user", limit=5)
    assert sql(node) == "SELECT * FROM user LIMIT 5"


def test_select_group_by() -> None:
    node = Select(
        columns=(ColumnRef("status"), Alias(FunctionCall("COUNT", (Star(),)), "cnt")),
        from_table="user",
        group_by=(ColumnRef("status"),),
    )
    assert sql(node) == "SELECT status, COUNT(*) AS cnt FROM user GROUP BY status"


def test_select_having() -> None:
    node = Select(
        columns=(ColumnRef("status"), Alias(FunctionCall("COUNT", (Star(),)), "cnt")),
        from_table="user",
        group_by=(ColumnRef("status"),),
        having=BinOp(FunctionCall("COUNT", (Star(),)), ">", Literal(5)),
    )
    assert sql(node) == (
        "SELECT status, COUNT(*) AS cnt FROM user GROUP BY status HAVING COUNT(*) > 5"
    )


def test_select_inner_join() -> None:
    node = Select(
        columns=(Star(),),
        from_table="order",
        from_alias="o",
        joins=(
            (
                "INNER",
                "user",
                "u",
                BinOp(ColumnRef("id", "u"), "=", ColumnRef("user_id", "o")),
            ),
        ),
    )
    assert sql(node) == 'SELECT * FROM "order" AS o INNER JOIN user AS u ON u.id = o.user_id'


def test_select_left_join_no_alias() -> None:
    node = Select(
        columns=(Star(),),
        from_table="order",
        joins=(
            (
                "LEFT",
                "user",
                None,
                BinOp(ColumnRef("id", "user"), "=", ColumnRef("user_id", "order")),
            ),
        ),
    )
    assert sql(node) == 'SELECT * FROM "order" LEFT JOIN user ON user.id = "order".user_id'


def test_select_multiple_order_by() -> None:
    node = Select(
        columns=(Star(),),
        from_table="product",
        order_by=(
            OrderItem(ColumnRef("category")),
            OrderItem(ColumnRef("price"), descending=True),
        ),
    )
    assert sql(node) == "SELECT * FROM product ORDER BY category ASC, price DESC"


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------


def test_insert_values() -> None:
    node = Insert(
        table="user",
        columns=("name", "email"),
        values=(Param(), Param()),
    )
    assert sql(node) == "INSERT INTO user (name, email) VALUES (?, ?)"


def test_insert_with_returning() -> None:
    node = Insert(
        table="user",
        columns=("name",),
        values=(Param(),),
        returning="id",
    )
    assert sql(node) == "INSERT INTO user (name) VALUES (?) RETURNING id"


def test_insert_select() -> None:
    sub = Select(
        columns=(ColumnRef("name"), ColumnRef("email")),
        from_table="temp_user",
    )
    node = Insert(table="user", columns=("name", "email"), values=sub)
    assert sql(node) == "INSERT INTO user (name, email) SELECT name, email FROM temp_user"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_no_where() -> None:
    node = Update(
        table="user",
        assignments=(("active", Literal(False)),),
    )
    assert sql(node) == "UPDATE user SET active = 0"


def test_update_with_where() -> None:
    node = Update(
        table="user",
        assignments=(("name", Param()), ("email", Param())),
        where=BinOp(ColumnRef("id"), "=", Param()),
    )
    assert sql(node) == "UPDATE user SET name = ?, email = ? WHERE id = ?"


def test_update_multiple_assignments() -> None:
    node = Update(
        table="product",
        assignments=(
            ("price", Literal(9.99)),
            ("stock", BinOp(ColumnRef("stock"), "-", Literal(1))),
        ),
    )
    assert sql(node) == "UPDATE product SET price = 9.99, stock = stock - 1"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_no_where() -> None:
    node = Delete(table="user")
    assert sql(node) == "DELETE FROM user"


def test_delete_with_where() -> None:
    node = Delete(
        table="user",
        where=BinOp(ColumnRef("id"), "=", Param()),
    )
    assert sql(node) == "DELETE FROM user WHERE id = ?"


# ---------------------------------------------------------------------------
# render() helper
# ---------------------------------------------------------------------------


def test_render_returns_tuple() -> None:
    node = Select(columns=(Star(),), from_table="t")
    result = render(node)
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_render_with_explicit_builder() -> None:
    node = Select(columns=(Star(),), from_table="t")
    sql_str, p = render(node, builder=SQLiteBuilder())
    assert sql_str == "SELECT * FROM t"
    assert p == []


def test_render_default_builder_is_sqlite() -> None:
    node = Select(columns=(Star(),), from_table="t")
    render(node)  # assert it doesn't raise


# ---------------------------------------------------------------------------
# Node immutability (dataclass frozen=True)
# ---------------------------------------------------------------------------


def test_nodes_are_frozen() -> None:
    node = ColumnRef("id")
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.column = "other"  # type: ignore[misc]


def test_param_default_name_is_none() -> None:
    assert Param().name is None


def test_select_defaults() -> None:
    s = Select(columns=(Star(),), from_table="t")
    assert s.where is None
    assert s.distinct is False
    assert s.joins == ()
    assert s.group_by == ()
    assert s.order_by == ()
    assert s.limit is None
    assert s.offset is None


def test_emit_dispatch_case_when_directly() -> None:
    """CaseWhen is dispatchable as a top-level node via render()."""
    # This exercises the CaseWhen arm in the _emit match/case dispatch,
    # which is normally bypassed because _emit_case calls _emit_case_when
    # directly.  Passing CaseWhen to render() triggers the dispatch arm.
    node = CaseWhen(Literal(1), Literal("a"))
    assert sql(node) == "WHEN 1 THEN 'a'"


def test_select_group_by_multiple_columns() -> None:
    """Covers the ``if i:`` comma path inside _emit_select group_by loop."""
    node = Select(
        columns=(ColumnRef("a"), ColumnRef("b"), FunctionCall("COUNT", (Star(),))),
        from_table="t",
        group_by=(ColumnRef("a"), ColumnRef("b")),
    )
    assert sql(node) == "SELECT a, b, COUNT(*) FROM t GROUP BY a, b"


def test_select_for_update() -> None:
    node = Select(columns=(Star(),), from_table="product", for_update=True)
    assert sql(node) == "SELECT * FROM product FOR UPDATE"


def test_select_for_update_skip_locked() -> None:
    node = Select(
        columns=(Star(),),
        from_table="product",
        for_update=True,
        for_update_skip_locked=True,
    )
    assert sql(node) == "SELECT * FROM product FOR UPDATE SKIP LOCKED"


# ---------------------------------------------------------------------------
# RawSql node and sql() helper
# ---------------------------------------------------------------------------


def test_raw_sql_no_params() -> None:
    node = sql_node("active = 1")
    assert isinstance(node, RawSql)
    s, p = render(node)
    assert s == "active = 1"
    assert p == []


def test_raw_sql_single_named_param_sqlite() -> None:
    node = sql_node("status = :s", s="active")
    s, p = render(node)
    assert s == "status = ?"
    assert p == ["active"]


def test_raw_sql_multiple_named_params_sqlite() -> None:
    node = sql_node("age > :lo AND age < :hi", lo=18, hi=65)
    s, p = render(node)
    assert s == "age > ? AND age < ?"
    assert p == [18, 65]


def test_raw_sql_format_builder() -> None:
    node = sql_node("status = :s", s="active")
    s, p = FormatBuilder().render(node)
    assert s == "status = %s"
    assert p == ["active"]


def test_raw_sql_in_filter_select() -> None:
    """RawSql can be embedded as a WHERE condition in a Select node."""
    where = sql_node("status = :s AND qty > :q", s="active", q=0)
    stmt = Select(columns=(Star(),), from_table="product", where=where)
    s, p = render(stmt)
    assert "status = ?" in s
    assert "qty > ?" in s
    assert p == ["active", 0]


def test_raw_sql_repeated_placeholder_order() -> None:
    """A named placeholder that appears multiple times is resolved in order."""
    node = sql_node("a = :x OR b = :y OR c = :x", x=1, y=2)
    s, p = render(node)
    # :x → ? appears at positions 1 and 3; :y → ? at position 2
    assert s == "a = ? OR b = ? OR c = ?"
    assert p == [1, 2, 1]


def test_sql_node_is_raw_sql_instance() -> None:
    node = sql_node("1=1")
    assert isinstance(node, RawSql)
    assert node.fragment == "1=1"
    assert node.params == {}


# ---------------------------------------------------------------------------
# FuncCall and ExistsNode emission (sql/builder.py lines 88, 90, 193-195, 198-199)
# ---------------------------------------------------------------------------

from nextorm.sql.nodes import ExistsNode, FuncCall  # noqa: E402


def test_func_call_node_rendered() -> None:
    """FuncCall node (e.g. UPPER(name)) is rendered via _emit_func_call."""
    node = FuncCall(func="UPPER", arg=ColumnRef("name"))
    s, p = render(node)
    assert s == "UPPER(name)"
    assert p == []


def test_exists_node_rendered() -> None:
    """ExistsNode is rendered via _emit_exists, including params."""
    node = ExistsNode(sql="SELECT 1 FROM t WHERE t.x = ?", params=("hello",))
    s, p = render(node)
    assert s == "EXISTS (SELECT 1 FROM t WHERE t.x = ?)"
    assert p == ["hello"]
