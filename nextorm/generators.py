"""Generator-expression query syntax.

Usage::

    from nextorm import Database, Entity, Req
    from nextorm.generators import select


    class Product(Entity):
        name: Req[str]
        price: Req[float]


    db = Database(entities=[Product])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    # Equivalent to db.select(Product).filter(Product.price > 100)
    results = select(p for p in Product if p.price > 100)

Supported filter expressions
-----------------------------
* Simple comparisons: ``p.attr == val``, ``p.attr > val``, etc.
* Logical conjunctions: ``p.a > 1 and p.b < 5``
* Logical disjunctions: ``p.a == 1 or p.b == 2``
* Negation: ``not p.active``

Limitations
-----------
The decompiler translates Python bytecode back to SQL AST nodes.  It
handles the common single-attribute comparison patterns.  Complex
Python expressions (function calls, multi-level attribute access, etc.)
are not supported and will raise :exc:`~nextorm.generators.DecompileError`.

The query is executed against the database that the entity was last
registered with (either through a :func:`~nextorm.session.db_session` or by
calling ``db.save()`` / ``db.select(...)`` at least once).  To use with a
specific database, prefer ``db.select(Entity).filter(...)`` instead.
"""

from __future__ import annotations

import dis
import sys
import types  # noqa: TC003
from typing import TYPE_CHECKING, Any, cast

from nextorm.entity import Entity

if TYPE_CHECKING:
    from collections.abc import Generator

    from nextorm.query import QuerySet
    from nextorm.sql.nodes import SqlNode

__all__ = ["select", "count", "avg", "sum", "min", "max", "DecompileError"]

# Mapping from Python compare-op bytecode names to SQL operators
_COMPARE_OPS: dict[str, str] = {
    "==": "=",
    "!=": "<>",
    "<": "<",
    "<=": "<=",
    ">": ">",
    ">=": ">=",
    "is": "IS",
    "is not": "IS NOT",
    "in": "IN",
    "not in": "NOT IN",
}

# Python 3.12+ uses integer rich-comparison codes in COMPARE_OP
_RICH_COMPARE_CODES: dict[int, str] = {
    0: "<",
    1: "<=",
    2: "==",
    3: "!=",
    4: ">",
    5: ">=",
}

# Python 3.11+ uses integer NB_* codes for BINARY_OP
_BINARY_OP_CODES: dict[int, str] = {
    0: "+",  # NB_ADD
    5: "*",  # NB_MULTIPLY
    10: "-",  # NB_SUBTRACT
    11: "/",  # NB_TRUE_DIVIDE
}


class DecompileError(Exception):
    """Raised when the bytecode decompiler cannot translate a generator expression."""


# ---------------------------------------------------------------------------
# Decompiler
# ---------------------------------------------------------------------------


class _StackItem:
    """A partial expression item on the decompiler's virtual stack."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: Any) -> None:
        self.kind = kind  # "node" | "attr" | "name"
        self.value = value


def _decompile_condition(code: types.CodeType, free_vars: dict[str, Any]) -> SqlNode | None:
    """Decompile the filter condition from a generator expression's code object.

    Parameters
    ----------
    code:
        The code object of the generator expression.
    free_vars:
        Mapping of free variable names to their values (from the enclosing scope).

    Returns
    -------
    SqlNode or None
        The SQL filter node, or ``None`` if the generator has no ``if`` clause.

    Raises
    ------
    DecompileError
        If the bytecode pattern is not supported.
    """
    from nextorm.sql.nodes import BinOp, ColumnRef, Param, UnaryOp  # noqa: PLC0415

    instructions = list(dis.get_instructions(code))
    stack: list[_StackItem] = []
    and_nodes: list[SqlNode] = []  # nodes in the current AND group
    or_groups: list[SqlNode] = []  # completed OR alternatives (each already AND-combined)

    def _finalize_and_group() -> None:
        """Combine and_nodes with AND, push result to or_groups, reset and_nodes."""
        if not and_nodes:
            return
        grp: SqlNode = and_nodes[0]
        for extra in and_nodes[1:]:
            grp = BinOp(grp, "AND", extra)
        or_groups.append(grp)
        and_nodes.clear()

    def pop() -> _StackItem:
        if not stack:  # pragma: no cover
            raise DecompileError("Unexpected empty stack during decompilation.")
        return stack.pop()

    def to_node(item: _StackItem) -> SqlNode:
        """Convert a stack item to a SqlNode."""
        if item.kind == "node":
            return cast("SqlNode", item.value)
        if item.kind == "attr":
            # Attribute access on the iter var → ColumnRef
            return ColumnRef(item.value)
        if item.kind == "name":
            # Resolved constant/variable
            return Param(value=item.value)
        raise DecompileError(f"Cannot convert stack item {item!r} to SqlNode.")  # pragma: no cover

    i = 0
    while i < len(instructions):
        instr = instructions[i]
        op = instr.opname

        if op in ("RESUME", "GEN_START"):
            i += 1
            continue

        if op in (
            "GET_ITER",
            "FOR_ITER",
            "END_FOR",
            "JUMP_BACKWARD",
            "JUMP_FORWARD",
            "RETURN_VALUE",
            "RETURN_CONST",
            "YIELD_VALUE",
            "LIST_APPEND",
            "STORE_FAST",
            "POP_TOP",
            "SWAP",
            "COPY",
            "NOP",
            "LOAD_FAST_CHECK",
            "CLEANUP_THROW",
            # Python 3.13 control-flow and exception-table opcodes
            "RETURN_GENERATOR",
            "CALL_INTRINSIC_1",
            "RERAISE",
            "LOAD_FAST_AND_CLEAR",
            # Python 3.13: bool coercion before POP_JUMP
            "TO_BOOL",
        ):
            i += 1
            continue

        if op == "STORE_FAST_LOAD_FAST":
            # Python 3.13 combined: store iteration var AND push it on stack.
            # argval is a tuple (store_name, load_name).
            load_name = instr.argval[1] if isinstance(instr.argval, tuple) else instr.argval  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
            stack.append(_StackItem("iter_var", load_name))
            i += 1
            continue

        if op in ("LOAD_FAST", "LOAD_FAST_BORROW"):
            # Iteration variable — push as placeholder.
            # LOAD_FAST_BORROW is the Python 3.14 borrow-semantics variant.
            stack.append(_StackItem("iter_var", instr.argval))
            i += 1
            continue

        if op in ("LOAD_DEREF", "LOAD_GLOBAL", "LOAD_NAME"):
            # Variable from enclosing scope — resolve its value
            name = instr.argval
            val = free_vars.get(name)
            stack.append(_StackItem("name", val))
            i += 1
            continue

        if op == "LOAD_CONST":
            stack.append(_StackItem("name", instr.argval))
            i += 1
            continue

        if op in ("LOAD_ATTR", "LOAD_METHOD"):
            if stack and stack[-1].kind == "iter_var":
                # p.attr → ColumnRef("attr")
                stack.pop()
                stack.append(_StackItem("attr", instr.argval))
            elif stack and stack[-1].kind == "attr":
                # Chained attribute: e.g. p.x.y — not supported
                raise DecompileError(
                    f"Multi-level attribute access not supported: {stack[-1].value}.{instr.argval}"
                )
            else:
                # Attribute on a bound value — skip for now
                pop()
                stack.append(_StackItem("attr", instr.argval))
            i += 1
            continue

        if op == "COMPARE_OP":
            right = pop()
            left = pop()
            # Map compare op to SQL operator
            arg = instr.argval
            if isinstance(arg, int):  # pragma: no cover — argval is str in Python 3.13
                sql_op = _RICH_COMPARE_CODES.get(arg & 0xF)
            else:
                sql_op = _COMPARE_OPS.get(str(arg).lower().rstrip(" ("))
            if sql_op is None:  # pragma: no cover
                raise DecompileError(f"Unsupported comparison operator: {arg!r}")
            node: SqlNode = BinOp(to_node(left), sql_op, to_node(right))
            stack.append(_StackItem("node", node))
            i += 1
            continue

        if op in ("BINARY_OP",):
            raw = instr.argval
            # Python 3.11+ uses integer NB_* codes; earlier versions use strings
            sql_op_b = _BINARY_OP_CODES.get(raw) if isinstance(raw, int) else raw
            right = pop()
            left = pop()
            if sql_op_b in ("+", "-", "*", "/"):
                node = BinOp(to_node(left), sql_op_b, to_node(right))
                stack.append(_StackItem("node", node))
            else:
                raise DecompileError(f"Unsupported binary op: {raw!r}")
            i += 1
            continue

        if op in ("UNARY_NOT",):  # pragma: no cover — Python 3.13 uses TO_BOOL+POP_JUMP
            operand = pop()
            node = UnaryOp("NOT", to_node(operand))
            stack.append(_StackItem("node", node))
            i += 1
            continue

        if op in (
            "POP_JUMP_IF_FALSE",
            "POP_JUMP_FORWARD_IF_FALSE",
            "POP_JUMP_IF_NONE",
            "POP_JUMP_IF_NOT_NONE",
        ):
            # AND condition opener: if false/None, skip this AND group.
            if stack:  # pragma: no branch
                item = pop()
                if item.kind == "node":
                    and_nodes.append(item.value)
            i += 1
            continue

        if op in (
            "POP_JUMP_IF_TRUE",
            "POP_JUMP_FORWARD_IF_TRUE",
        ):
            # In Python 3.13 generator filters, POP_JUMP_IF_TRUE is used for both
            # AND steps ("if true, proceed to next condition") and OR triggers
            # ("if true, jump directly to yield site").
            #
            # Distinguishing rule: look past any NOT_TAKEN hints to find the next
            # meaningful instruction.
            # - If next == JUMP_BACKWARD  → this is an AND condition (the true path
            #   continues evaluation; the false path hits JUMP_BACKWARD to skip yield).
            # - If next != JUMP_BACKWARD  → this is an OR trigger (the true path
            #   jumps to yield; falling through means continuing with the next OR alt).
            #
            # Python 3.14 inserts a NOT_TAKEN hint between POP_JUMP_IF_TRUE and
            # JUMP_BACKWARD, so we skip over NOT_TAKEN before checking.
            if stack:  # pragma: no branch
                item = pop()
                if item.kind == "node":
                    and_nodes.append(item.value)
            j = i + 1
            while j < len(instructions) and instructions[j].opname == "NOT_TAKEN":
                j += 1
            next_op = instructions[j].opname if j < len(instructions) else ""
            if next_op != "JUMP_BACKWARD":
                _finalize_and_group()
            i += 1
            continue

        if op == "JUMP_IF_FALSE_OR_POP":  # pragma: no cover — Python 3.13 uses POP_JUMP
            # short-circuit AND (Python ≤ 3.12 style)
            if stack:
                item = pop()
                if item.kind == "node":
                    and_nodes.append(item.value)
            i += 1
            continue

        if op == "JUMP_IF_TRUE_OR_POP":  # pragma: no cover — Python 3.13 uses POP_JUMP
            # short-circuit OR (Python ≤ 3.12 style) — seal current AND group
            if stack:
                item = pop()
                if item.kind == "node":
                    and_nodes.append(item.value)
            _finalize_and_group()

        if op in ("IS_OP",):  # pragma: no cover — Python 3.13 uses POP_JUMP_IF_NONE
            right = pop()
            left = pop()
            sql_op = "IS NOT" if instr.argval else "IS"
            node = BinOp(to_node(left), sql_op, to_node(right))
            stack.append(_StackItem("node", node))
            i += 1
            continue

        if op in ("CONTAINS_OP",):
            right = pop()
            left = pop()
            sql_op = "NOT IN" if instr.argval else "IN"
            node = BinOp(to_node(left), sql_op, to_node(right))
            stack.append(_StackItem("node", node))
            i += 1
            continue

        # Any remaining instructions on the right-hand-side path are ignored;
        # raise if they look significant
        if op.startswith(("CALL", "BUILD_", "MAKE_")):
            raise DecompileError(
                f"Unsupported bytecode instruction {op!r} in select() filter. "
                "Use db.select(Entity).filter(...) for complex conditions."
            )

        i += 1

    # Finalise any remaining AND group, then combine all OR alternatives
    _finalize_and_group()
    if not or_groups:
        return None
    result: SqlNode = or_groups[0]
    for extra in or_groups[1:]:
        result = BinOp(result, "OR", extra)
    return result


# ---------------------------------------------------------------------------
# Public select() function
# ---------------------------------------------------------------------------


def select[T: Entity](gen: Generator[T, None, None]) -> QuerySet[T]:
    """Execute a generator-expression query and return a :class:`~nextorm.query.QuerySet`.

    The generator expression must iterate over a single entity class::

        select(p for p in Product if p.price > 100)

    This is syntactic sugar for::

        db.select(Product).filter(Product.price > 100)

    The database to use is determined by inspecting the ``_db_`` attribute on
    the entity class's iterator (set from :meth:`EntityMeta.__iter__`).  The
    entity class must have been registered with a bound, mapped database.

    Raises
    ------
    DecompileError
        If the filter condition cannot be decompiled.
    RuntimeError
        If the entity class has no associated database context.
    """
    from nextorm.entity import _EntityIterator  # noqa: PLC0415

    assert isinstance(gen, types.GeneratorType)
    # The generator's frame locals contain the iterator as '.0'
    gi_frame = gen.gi_frame
    if gi_frame is None:
        raise RuntimeError(
            "Generator has already been exhausted. Pass a fresh generator expression to select()."
        )
    iterator = gi_frame.f_locals.get(".0")
    if not isinstance(iterator, _EntityIterator):
        raise RuntimeError(
            "select() requires a generator iterating over an Entity class, "
            f"e.g. select(p for p in MyEntity if ...). Got: {type(iterator)!r}"
        )
    entity_meta = iterator.entity_cls  # EntityMeta; __name__ resolves via type metaclass
    entity_name: str = entity_meta.__name__
    entity_cls = cast("type[T]", entity_meta)  # for db.select() which takes type[T]

    # Find a database that has this entity mapped
    from nextorm.database import Database  # noqa: PLC0415

    db: Database | None = None
    # Look for a db_session's identity-cached instance or a globally bound DB
    # We use a simple heuristic: find the first registered Database that has
    # the entity in its schema (checked via class-level _db_ attribute or
    # the global registry approach).
    # Since entities don't store db references globally, we inspect sys.modules
    # for any Database which has generated mapping for this entity.
    from nextorm.database import Database as _DB  # noqa: PLC0415

    for mod in list(sys.modules.values()):
        for attr in list(vars(mod).values()):
            if isinstance(attr, _DB) and entity_name.lower() in attr.schema:
                db = attr
                break
        if db is not None:
            break

    if db is None:
        raise RuntimeError(
            f"Cannot find a mapped Database for entity {entity_name!r}."
            " Call db.generate_mapping() with this entity first."
        )

    # Decompile the filter condition from the generator's code object
    code: types.CodeType = gen.gi_code
    # Collect free variables (enclosing scope) for constant resolution
    free_vars: dict[str, Any] = {**gi_frame.f_globals, **gi_frame.f_locals}

    condition = _decompile_condition(code, free_vars)

    qs = db.select(entity_cls)
    if condition is not None:
        qs = qs.filter(condition)
    return qs


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------


def _decompile_yield_attr(code: types.CodeType) -> str | None:
    """Return the attribute name the generator yields (``p.attr``), or ``None``.

    Scans backwards from the ``YIELD_VALUE`` instruction.  If the instruction
    immediately before it is a ``LOAD_ATTR`` (e.g. ``p.price``), the attribute
    name is returned.  If the generator yields the entity itself (``p``), or the
    yield expression cannot be determined, returns ``None``.
    """
    instructions = list(dis.get_instructions(code))
    yield_idx: int | None = None
    for i, instr in enumerate(instructions):
        if instr.opname == "YIELD_VALUE":
            yield_idx = i
            break
    if yield_idx is None:  # pragma: no cover
        return None
    for j in range(yield_idx - 1, -1, -1):
        op = instructions[j].opname
        if op in ("LOAD_ATTR", "LOAD_METHOD"):
            return str(instructions[j].argval)
        if op in (
            "LOAD_FAST",
            "STORE_FAST",
            "STORE_FAST_LOAD_FAST",
            "GET_ITER",
            "FOR_ITER",
        ):
            # Reached iteration machinery — the generator yields the entity itself
            return None
        if op.startswith("POP_JUMP"):  # pragma: no cover
            # Backed up into the filter area — entity is being yielded
            return None
    return None  # pragma: no cover


def count[T: Entity](gen: Generator[T, None, None]) -> int:
    """Return the number of entities matching the generator-expression filter.

    Example::

        n = count(p for p in Product if p.price > 100)

    Equivalent to::

        db.select(Product).filter(Product.price > 100).count()
    """
    return select(gen).count()


def avg(gen: Generator[Any, None, None]) -> Any:
    """Compute ``AVG`` of the attribute yielded by the generator expression.

    The generator must yield a field attribute, not the entity itself::

        mean_price = avg(p.price for p in Product if p.active)

    Equivalent to::

        db.select(Product).filter(Product.active == True).avg("price")

    Raises
    ------
    DecompileError
        If the generator yields the entity rather than a field attribute.
    """
    assert isinstance(gen, types.GeneratorType)
    field_name = _decompile_yield_attr(gen.gi_code)
    if field_name is None:
        raise DecompileError(
            "avg() requires a generator that yields a field attribute, "
            "e.g. avg(p.price for p in Product).  "
            "To count rows use count(p for p in Product)."
        )
    qs = select(gen)
    return qs.avg(field_name)


def sum(gen: Generator[Any, None, None]) -> Any:  # noqa: A001
    """Compute ``SUM`` of the attribute yielded by the generator expression.

    The generator must yield a field attribute, not the entity itself::

        total = sum(p.price for p in Product if p.in_stock)

    Equivalent to::

        db.select(Product).filter(Product.in_stock == True).sum("price")

    Raises
    ------
    DecompileError
        If the generator yields the entity rather than a field attribute.
    """
    assert isinstance(gen, types.GeneratorType)
    field_name = _decompile_yield_attr(gen.gi_code)
    if field_name is None:
        raise DecompileError(
            "sum() requires a generator that yields a field attribute, "
            "e.g. sum(p.price for p in Product).  "
            "To count rows use count(p for p in Product)."
        )
    qs = select(gen)
    return qs.sum(field_name)


def min(gen: Generator[Any, None, None]) -> Any:  # noqa: A001
    """Compute ``MIN`` of the attribute yielded by the generator expression.

    The generator must yield a field attribute, not the entity itself::

        cheapest = min(p.price for p in Product)

    Equivalent to::

        db.select(Product).min("price")

    Raises
    ------
    DecompileError
        If the generator yields the entity rather than a field attribute.
    """
    assert isinstance(gen, types.GeneratorType)
    field_name = _decompile_yield_attr(gen.gi_code)
    if field_name is None:
        raise DecompileError(
            "min() requires a generator that yields a field attribute, "
            "e.g. min(p.price for p in Product).  "
            "To count rows use count(p for p in Product)."
        )
    qs = select(gen)
    return qs.min(field_name)


def max(gen: Generator[Any, None, None]) -> Any:  # noqa: A001
    """Compute ``MAX`` of the attribute yielded by the generator expression.

    The generator must yield a field attribute, not the entity itself::

        priciest = max(p.price for p in Product)

    Equivalent to::

        db.select(Product).max("price")

    Raises
    ------
    DecompileError
        If the generator yields the entity rather than a field attribute.
    """
    assert isinstance(gen, types.GeneratorType)
    field_name = _decompile_yield_attr(gen.gi_code)
    if field_name is None:
        raise DecompileError(
            "max() requires a generator that yields a field attribute, "
            "e.g. max(p.price for p in Product).  "
            "To count rows use count(p for p in Product)."
        )
    qs = select(gen)
    return qs.max(field_name)
