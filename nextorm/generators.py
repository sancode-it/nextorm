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

import dataclasses
import dis
import sys
import types  # noqa: TC003
from typing import TYPE_CHECKING, Any, cast

from nextorm.entity import Entity

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from nextorm.query import QuerySet
    from nextorm.sql.nodes import SqlNode

__all__ = ["select", "count", "avg", "sum", "min", "max", "DecompileError"]


@dataclasses.dataclass(frozen=True)
class _JoinSpec:
    """Describes a JOIN that must be added to the QuerySet for a relation traversal."""

    from_table: str
    table_name: str
    on: Any  # BinOp at runtime
    join_type: str = "INNER"


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


def _decompile_condition(
    code: types.CodeType,
    free_vars: dict[str, Any],
    entity_cls: type[Entity] | None = None,
    func_globals: dict[str, Any] | None = None,
) -> tuple[SqlNode | None, list[_JoinSpec]]:
    """Decompile the filter condition from a generator expression's code object.

    Parameters
    ----------
    code:
        The code object of the generator expression or lambda predicate.
    free_vars:
        Mapping of free variable names to their values (from the enclosing scope).
    entity_cls:
        The entity class being queried.  Required to resolve two-level attribute
        chains such as ``lambda c: c.shop.slug == slug`` into a JOIN + column
        reference.  When ``None``, chained attribute access raises
        :exc:`DecompileError`.

    Returns
    -------
    tuple[SqlNode or None, list[_JoinSpec]]
        The SQL filter node (or ``None`` when the predicate has no condition)
        and a list of JOIN specifications that must be applied to the QuerySet
        before the filter condition.

    Raises
    ------
    DecompileError
        If the bytecode pattern is not supported.
    """
    from nextorm.sql.nodes import (  # noqa: PLC0415
        BinOp,
        ColumnRef,
        FuncCall,
        Literal,
        Param,
        UnaryOp,
    )

    instructions = list(dis.get_instructions(code))
    stack: list[_StackItem] = []
    and_nodes: list[SqlNode] = []  # nodes in the current AND group
    or_groups: list[SqlNode] = []  # completed OR alternatives (each already AND-combined)
    joins: list[_JoinSpec] = []  # JOINs required by relation traversals
    # Track whether the code yields a value (generator expression) vs returns one (lambda predicate).
    # Bare "attr" items left on the stack are filter conditions only in predicate (non-yield) code.
    has_yield = any(instr.opname == "YIELD_VALUE" for instr in instructions)

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
            # Attribute access on the iter var → ColumnRef (use the proper SQL column name)
            attr_name = cast("str", item.value)
            if entity_cls is not None:
                _fields = getattr(entity_cls, "_fields_", {})
                _relations = getattr(entity_cls, "_relations_", {})
                if attr_name in _fields:
                    col = _fields[attr_name].spec.column or attr_name
                    return ColumnRef(col, entity_cls._table_name_)
                if attr_name in _relations:
                    _ri = _relations[attr_name]
                    col = _ri.spec.column or f"{attr_name}_id"
                    return ColumnRef(col, entity_cls._table_name_)
            return ColumnRef(attr_name)
        if item.kind == "rel_chain":
            # N-level attribute access: p.rel1.rel2....field
            # item.value is a list of attribute names; the last one is the field,
            # all preceding ones are relation traversal steps.
            chain = cast("list[str]", item.value)
            if entity_cls is None:
                raise DecompileError(
                    f"Cannot resolve '{'.'.join(chain)}': entity class is required "
                    "for multi-level attribute access. Pass entity_cls to _decompile_condition."
                )
            from nextorm.entity import (  # noqa: PLC0415
                _matches_entity,
                _resolve_entity_target,
            )

            current_entity: Any = entity_cls
            current_table: str = entity_cls._table_name_
            # Walk all but the last name, building JOINs for each relation step.
            for rel_name in chain[:-1]:
                ri = current_entity._relations_.get(rel_name)
                if ri is None:
                    raise DecompileError(
                        f"'{rel_name}' is not a known relation on {current_entity.__name__!r}."
                    )
                target_cls = _resolve_entity_target(ri.spec.target)
                if target_cls is None:
                    raise DecompileError(
                        f"Cannot resolve target entity for relation '{rel_name}' "
                        f"on {current_entity.__name__!r}."
                    )
                target_table = target_cls._table_name_
                pk_fields = target_cls._pk_fields_
                if not pk_fields:  # pragma: no cover
                    raise DecompileError(f"Target entity {target_cls.__name__!r} has no primary key.")
                is_composite_target = len(pk_fields) > 1

                # Detect non-owning Single (FK is on target, not current).
                # This happens with reverse O2O relations where the other side has
                # primary_key=True (e.g., Config.shop: PK[Shop]).
                from nextorm.fields import RelationKind  # noqa: PLC0415

                is_non_owning = ri.spec.owner is False
                if not is_non_owning and ri.spec.owner is None:
                    # Auto-detect: target has a Single/PK relation back with primary_key=True
                    for rev_ri in target_cls._relations_.values():
                        if (
                            rev_ri.spec.kind == RelationKind.SINGLE
                            and _matches_entity(rev_ri.spec.target, current_entity)
                            and rev_ri.spec.primary_key
                        ):
                            is_non_owning = True
                            break

                if is_non_owning:
                    # FK is on target table.  Find the reverse relation column.
                    rev_fk_col: str | None = None
                    for rev_ri in target_cls._relations_.values():
                        if rev_ri.spec.kind == RelationKind.SINGLE and _matches_entity(
                            rev_ri.spec.target, current_entity
                        ):
                            rev_fk_col = rev_ri.spec.column or f"{rev_ri.name}_id"
                            break
                    if rev_fk_col is None:
                        rev_fk_col = f"{current_entity.__name__.lower()}_id"
                    # Also get the PK of current_entity for the join condition
                    cur_pk_fields = current_entity._pk_fields_
                    cur_pk_attr = cur_pk_fields[0] if cur_pk_fields else "id"
                    if cur_pk_attr in current_entity._fields_:
                        cur_pk_col = current_entity._fields_[cur_pk_attr].spec.column or cur_pk_attr
                    else:
                        cur_pk_col = f"{cur_pk_attr}_id"
                    join_cond = BinOp(
                        ColumnRef(rev_fk_col, target_table),
                        "=",
                        ColumnRef(cur_pk_col, current_table),
                    )
                elif is_composite_target:
                    # Composite PK target — build multi-column JOIN condition.
                    from nextorm.entity import (
                        _derive_composite_fk_cols,  # noqa: PLC0415
                    )

                    if ri.spec.columns:
                        fk_col_names = list(ri.spec.columns)
                    else:
                        fk_col_names = _derive_composite_fk_cols(rel_name, target_cls)
                    # Derive the target PK column names
                    target_pk_cols: list[str] = []
                    for pk_f in pk_fields:
                        if pk_f in target_cls._fields_:
                            target_pk_cols.append(target_cls._fields_[pk_f].spec.column or pk_f)
                        else:
                            trel = target_cls._relations_.get(pk_f)
                            target_pk_cols.append(
                                (trel.spec.column if trel else None) or f"{pk_f}_id"
                            )
                    # Build AND of individual column equalities
                    join_cond = BinOp(
                        ColumnRef(fk_col_names[0], current_table),
                        "=",
                        ColumnRef(target_pk_cols[0], target_table),
                    )
                    for fk_c, pk_c in zip(fk_col_names[1:], target_pk_cols[1:], strict=False):
                        join_cond = BinOp(
                            join_cond,
                            "AND",
                            BinOp(
                                ColumnRef(fk_c, current_table),
                                "=",
                                ColumnRef(pk_c, target_table),
                            ),
                        )
                else:
                    fk_col = ri.spec.column or f"{rel_name}_id"
                    pk_attr = pk_fields[0]  # pyright: ignore[reportGeneralTypeIssues]
                    if pk_attr in target_cls._fields_:
                        pk_col = target_cls._fields_[pk_attr].spec.column or pk_attr
                    else:
                        rel_pk = target_cls._relations_.get(pk_attr)
                        pk_col = (rel_pk.spec.column if rel_pk else None) or f"{pk_attr}_id"
                    join_cond = BinOp(
                        ColumnRef(fk_col, current_table),
                        "=",
                        ColumnRef(pk_col, target_table),
                    )
                # Dedup: only add the join once per (source_table, target_table) pair.
                join_key = (current_table, target_table)
                if not any((j.from_table, j.table_name) == join_key for j in joins):
                    joins.append(
                        _JoinSpec(
                            from_table=current_table,
                            table_name=target_table,
                            on=join_cond,
                        )
                    )
                current_entity = target_cls
                current_table = target_table
            # The last name is the field to reference on the terminal entity.
            field_name = chain[-1]
            if field_name in current_entity._fields_:
                col = current_entity._fields_[field_name].spec.column or field_name
            elif field_name in current_entity._relations_:
                ri2 = current_entity._relations_[field_name]
                col = ri2.spec.column or f"{field_name}_id"
            else:
                col = field_name
            return ColumnRef(col, current_table)
        if item.kind == "name":
            # Resolved constant/variable — extract PK if value is an Entity instance
            val = item.value
            if isinstance(val, Entity):
                from nextorm.database import _get_pk_val  # noqa: PLC0415

                val = _get_pk_val(val)
            return Param(value=val)
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
            # Python 3.14: loop-iteration cleanup (END_FOR equivalent)
            "POP_ITER",
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
            if name in free_vars:
                val: Any = free_vars[name]
            elif func_globals is not None and name in func_globals:
                val = func_globals[name]
            else:
                val = None
            stack.append(_StackItem("name", val))
            i += 1
            continue

        if op in ("LOAD_CONST", "LOAD_SMALL_INT"):
            # LOAD_SMALL_INT is the Python 3.14 optimized opcode for small integers
            stack.append(_StackItem("name", instr.argval))
            i += 1
            continue

        if op in ("LOAD_ATTR", "LOAD_METHOD"):
            if stack and stack[-1].kind == "iter_var":
                # p.attr → ColumnRef("attr")
                stack.pop()
                stack.append(_StackItem("attr", instr.argval))
            elif stack and stack[-1].kind == "attr":
                # Chained attribute: p.relation.field → rel_chain (start a chain)
                prev_attr = stack.pop().value
                stack.append(_StackItem("rel_chain", [prev_attr, instr.argval]))
            elif stack and stack[-1].kind == "rel_chain":
                # Extend an existing chain: p.a.b → p.a.b.c
                chain = list(cast("list[str]", stack[-1].value))
                chain.append(instr.argval)
                stack[-1] = _StackItem("rel_chain", chain)
            else:
                # Attribute on a bound value — resolve the attribute at decompile time
                prev = pop()
                if prev.kind == "name" and prev.value is not None:
                    resolved = getattr(prev.value, instr.argval, None)
                    stack.append(_StackItem("name", resolved))
                else:
                    stack.append(_StackItem("attr", instr.argval))
            i += 1
            continue

        if op == "COMPARE_OP":
            right = pop()
            left = pop()
            # Map compare op to SQL operator
            arg = instr.argval
            if isinstance(arg, int):
                sql_op = _RICH_COMPARE_CODES.get(arg & 0xF)  # pragma: no cover
            else:
                sql_op = _COMPARE_OPS.get(str(arg).lower().rstrip(" ("))
            if sql_op is None:  # pragma: no cover
                raise DecompileError(f"Unsupported comparison operator: {arg!r}")  # pragma: no cover
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

        if op in ("UNARY_NOT",):  # pragma: no cover
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
            if stack:  # pragma: no cover
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
            if stack:  # pragma: no cover
                item = pop()
                if item.kind == "node":
                    and_nodes.append(item.value)
            j = i + 1
            while j < len(instructions) and instructions[j].opname == "NOT_TAKEN":
                j += 1  # pragma: no cover
            next_op = instructions[j].opname if j < len(instructions) else ""
            if next_op != "JUMP_BACKWARD":
                _finalize_and_group()
            i += 1
            continue

        if op == "JUMP_IF_FALSE_OR_POP":  # pragma: no cover
            # short-circuit AND (Python ≤ 3.12 style)
            if stack:
                item = pop()
                if item.kind == "node":
                    and_nodes.append(item.value)
            i += 1
            continue

        if op == "JUMP_IF_TRUE_OR_POP":  # pragma: no cover
            # short-circuit OR (Python ≤ 3.12 style) — seal current AND group
            if stack:
                item = pop()
                if item.kind == "node":
                    and_nodes.append(item.value)
            _finalize_and_group()

        if op in ("IS_OP",):  # pragma: no cover
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

            # Special case: `val in col.lower()` → SQL `LOWER(col) LIKE '%val%'`
            if right.kind == "func_call" and sql_op == "IN":
                func_name, col_item = right.value
                col_node = to_node(col_item)
                val = left.value if left.kind == "name" else None
                if val is not None and isinstance(val, str):
                    val_lower = val.lower() if func_name == "LOWER" else val
                    like_node = BinOp(
                        FuncCall(func_name, col_node),
                        "LIKE",
                        Param(value=f"%{val_lower}%"),
                    )
                    stack.append(_StackItem("node", like_node))
                    i += 1
                    continue

            # M1: `entity in set_relation` → EXISTS (SELECT 1 FROM join_table ...)
            # e.g. `tag in s.tag_list` where tag_list is a M2M Set[ShopTag]
            if sql_op == "IN" and right.kind == "attr" and entity_cls is not None:
                attr_name = right.value
                rel = getattr(entity_cls, "_relations_", {}).get(attr_name)
                if rel is not None:
                    from nextorm.entity import (  # noqa: PLC0415
                        _matches_entity,
                        _resolve_entity_target,
                    )
                    from nextorm.fields import RelationKind  # noqa: PLC0415

                    if rel.spec.kind == RelationKind.SET:
                        target_cls = _resolve_entity_target(rel.spec.target)
                        owner_table = entity_cls._table_name_
                        if target_cls is not None:
                            target_table = target_cls._table_name_

                            # Determine if M2M (target also has Set back at owner)
                            is_m2m = any(
                                r.spec.kind == RelationKind.SET
                                and _matches_entity(r.spec.target, entity_cls)
                                for r in target_cls._relations_.values()
                            )

                            if is_m2m:
                                join_table = rel.spec.table or "_".join(
                                    sorted([owner_table, target_table])
                                )
                                owner_pk_fields = entity_cls._pk_fields_
                                owner_pk_col = (
                                    entity_cls._fields_[owner_pk_fields[0]].spec.column
                                    or owner_pk_fields[0]
                                    if owner_pk_fields and owner_pk_fields[0] in entity_cls._fields_
                                    else "id"
                                )
                                jt_owner_col = f"{owner_table}_id"
                                jt_target_col = f"{target_table}_id"
                                # Get the target entity's PK value from left
                                val = left.value
                                if isinstance(val, Entity):
                                    from nextorm.database import (
                                        _get_pk_val,  # noqa: PLC0415
                                    )

                                    val = _get_pk_val(val)
                                exists_sql = (
                                    f"SELECT 1 FROM {join_table} WHERE "
                                    f"{join_table}.{jt_owner_col} = {owner_table}.{owner_pk_col} AND "
                                    f"{join_table}.{jt_target_col} = ?"
                                )
                                from nextorm.sql.nodes import (
                                    ExistsNode,  # noqa: PLC0415
                                )

                                exists_node = ExistsNode(sql=exists_sql, params=(val,))
                                stack.append(_StackItem("node", exists_node))
                                i += 1
                                continue

            # M2: `val in set_relation.field` → EXISTS (SELECT 1 FROM target ...)
            # e.g. `True in p.variations.active` where variations is Set[ProductVariation]
            if sql_op == "IN" and right.kind == "rel_chain" and entity_cls is not None:
                chain = list(right.value)
                rel_name = chain[-2]
                field_name = chain[-1]
                rel = getattr(entity_cls, "_relations_", {}).get(rel_name)
                if rel is not None:
                    from nextorm.entity import _resolve_entity_target  # noqa: PLC0415
                    from nextorm.fields import RelationKind  # noqa: PLC0415

                    if rel.spec.kind == RelationKind.SET:
                        target_cls = _resolve_entity_target(rel.spec.target)
                        owner_table = entity_cls._table_name_
                        if target_cls is not None:
                            target_table = target_cls._table_name_
                            owner_pk_fields = entity_cls._pk_fields_
                            owner_pk_col = (
                                entity_cls._fields_[owner_pk_fields[0]].spec.column
                                or owner_pk_fields[0]
                                if owner_pk_fields and owner_pk_fields[0] in entity_cls._fields_
                                else "id"
                            )
                            # Find back-ref FK col on target
                            from nextorm.entity import _matches_entity  # noqa: PLC0415

                            back_ref = next(
                                (
                                    r
                                    for r in target_cls._relations_.values()
                                    if r.spec.kind == RelationKind.SINGLE
                                    and _matches_entity(r.spec.target, entity_cls)
                                ),
                                None,
                            )
                            fk_col = (
                                (back_ref.spec.column or f"{back_ref.name}_id")
                                if back_ref
                                else f"{rel_name}_id"
                            )
                            # Get field column
                            target_fi = target_cls._fields_.get(field_name)
                            field_col = (
                                (target_fi.spec.column or field_name) if target_fi else field_name
                            )
                            val = left.value if left.kind == "name" else None
                            from nextorm.fields import _serialize_value  # noqa: PLC0415

                            if val is not None:
                                val = _serialize_value(val)
                            exists_sql = (
                                f"SELECT 1 FROM {target_table} WHERE "
                                f"{target_table}.{fk_col} = {owner_table}.{owner_pk_col} AND "
                                f"{target_table}.{field_col} = ?"
                            )
                            from nextorm.sql.nodes import ExistsNode  # noqa: PLC0415

                            exists_node = ExistsNode(sql=exists_sql, params=(val,))
                            stack.append(_StackItem("node", exists_node))
                            i += 1
                            continue

            node = BinOp(to_node(left), sql_op, to_node(right))
            stack.append(_StackItem("node", node))
            i += 1
            continue

        # Any remaining instructions on the right-hand-side path are ignored;
        # raise if they look significant
        if op.startswith("CALL"):
            # Attempt to evaluate a free-variable function call at decompile time.
            # Works for calls like datetime.now(), uuid4(), len(x), etc.
            # The CALL N instruction expects: [..., func, arg0, ..., argN-1]
            # (PUSH_NULL before the function is ignored and not on our stack).
            n_args = instr.argval if isinstance(instr.argval, int) else 0
            # Pop arguments (top of stack = last arg)
            args: list[_StackItem] = []
            for _ in range(n_args):
                a = pop()
                if a.kind not in ("name", "attr", "rel_chain"):
                    raise DecompileError(
                        f"Unsupported bytecode instruction {op!r} in select() filter: "
                        f"non-constant argument of kind {a.kind!r}. "
                        "Use db.select(Entity).filter(...) for complex conditions."
                    )
                args.insert(0, a)
            # Pop the function
            func_item = pop()

            # Case 1: column method call — e.g. `u.email.lower()` → FuncCall("LOWER", col)
            _COL_METHODS = {"lower": "LOWER", "upper": "UPPER", "strip": "TRIM"}
            if n_args == 0 and func_item.kind == "rel_chain":
                chain = list(func_item.value)
                method_name = chain[-1]
                if method_name in _COL_METHODS:
                    col_chain = chain[:-1]
                    sql_func = _COL_METHODS[method_name]
                    col_item = _StackItem(
                        "rel_chain" if len(col_chain) > 1 else "attr",
                        col_chain if len(col_chain) > 1 else col_chain[0],
                    )
                    stack.append(_StackItem("func_call", (sql_func, col_item)))
                    i += 1
                    continue
                raise DecompileError(
                    f"Unsupported column method '{method_name}' in select() filter. "
                    "Use db.select(Entity).filter(...) for complex conditions."
                )
            if n_args == 0 and func_item.kind == "attr":
                method_name = func_item.value
                if method_name in _COL_METHODS:
                    raise DecompileError(
                        f"Column method {method_name!r} requires attribute"
                        " context (e.g. u.field.lower())."
                    )

            # Case 2: free-variable function call — evaluate at compile time
            if func_item.kind != "name" or not callable(func_item.value):
                raise DecompileError(
                    f"Unsupported bytecode instruction {op!r} in select() filter. "
                    "Use db.select(Entity).filter(...) for complex conditions."
                )
            # All args must be resolved "name" items
            evaluated_args = []
            for a in args:
                if a.kind == "name":
                    evaluated_args.append(a.value)  # pyright: ignore[reportUnknownMemberType]
                else:
                    raise DecompileError(
                        f"Unsupported bytecode instruction {op!r} in select() filter: "
                        f"non-constant argument. "
                        "Use db.select(Entity).filter(...) for complex conditions."
                    )
            try:
                call_result = func_item.value(*evaluated_args)
            except Exception as exc:
                raise DecompileError(
                    f"Error evaluating free-variable call at decompile time: {exc}"
                ) from exc
            stack.append(_StackItem("name", call_result))
            i += 1
            continue

        if op.startswith(("BUILD_", "MAKE_")):
            raise DecompileError(
                f"Unsupported bytecode instruction {op!r} in select() filter. "
                "Use db.select(Entity).filter(...) for complex conditions."
            )

        i += 1

    # Collect any node items remaining on the stack.  In generator expressions,
    # comparison nodes are moved to and_nodes by POP_JUMP_IF_FALSE.  In a
    # plain lambda (no jump instructions), the final comparison node stays on
    # the stack and must be drained here before finalising.
    for item in stack:
        if item.kind == "node":
            and_nodes.append(item.value)
        elif item.kind == "attr" and not has_yield:
            # Bare attribute used as a boolean condition in a predicate (non-generator)
            # e.g. ``lambda pv: pv.product == x and pv.is_default``.
            # In generator expressions the trailing attr is the yielded value — not a filter.
            and_nodes.append(BinOp(to_node(item), "=", Literal(value=True)))

    # Finalise any remaining AND group, then combine all OR alternatives
    _finalize_and_group()
    if not or_groups:
        return None, joins
    result: SqlNode = or_groups[0]
    for extra in or_groups[1:]:
        result = BinOp(result, "OR", extra)
    return result, joins


# ---------------------------------------------------------------------------
# Public select() function
# ---------------------------------------------------------------------------


def select[ET: Entity](gen: Generator[ET, None, None]) -> QuerySet[ET]:
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
    entity_cls = cast("type[ET]", entity_meta)  # for db.select() which takes type[ET]

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

    condition, joins = _decompile_condition(code, free_vars, entity_cls=entity_cls)

    qs = db.select(entity_cls)
    for j in joins:
        qs = qs.join(j.table_name, j.on, join_type=j.join_type)
    if condition is not None:
        qs = qs.filter(condition)
    return qs


def _apply_predicate(  # pyright: ignore[reportUnusedFunction]
    qs: Any,
    predicate: Callable[[Any], Any],
    entity_cls: type[Entity],
) -> Any:
    """Apply a callable predicate to *qs*, resolving relation JOINs automatically.

    Used internally by :meth:`~nextorm.entity.Entity.get`,
    :meth:`~nextorm.entity.Entity.exists`, and
    :meth:`~nextorm.entity.Entity.aget`.  Decompiles the predicate's bytecode
    and adds any required JOIN clauses before applying the filter condition.

    Parameters
    ----------
    qs:
        The base :class:`~nextorm.query.QuerySet` to augment.
    predicate:
        A callable (lambda or function) whose bytecode is decompiled into
        SQL filter nodes.
    entity_cls:
        The entity class being queried — needed to resolve relation traversals.
    """
    code = predicate.__code__
    free_vars: dict[str, Any] = {}
    if code.co_freevars and predicate.__closure__:
        for name, cell in zip(code.co_freevars, predicate.__closure__, strict=True):
            free_vars[name] = cell.cell_contents
    func_globals: dict[str, Any] | None = getattr(predicate, "__globals__", None)
    condition, joins = _decompile_condition(
        code, free_vars, entity_cls=entity_cls, func_globals=func_globals
    )
    for j in joins:
        qs = qs.join(j.table_name, j.on, join_type=j.join_type)
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


def _is_entity_generator(gen: Generator[Any, None, None]) -> bool:
    """Return True when *gen* is iterating over an ``Entity`` class (via ``_EntityIterator``).

    Used by the aggregation helpers (``sum``, ``avg``, ``min``, ``max``,
    ``count``) to decide whether to run the generator through the ORM query
    pipeline or fall back to the plain Python built-in.
    """
    from nextorm.entity import _EntityIterator  # noqa: PLC0415

    if not isinstance(gen, types.GeneratorType):
        return False
    gi_frame = gen.gi_frame
    if gi_frame is None:
        return False
    iterator = gi_frame.f_locals.get(".0")
    return isinstance(iterator, _EntityIterator)


def count(gen: Any) -> int:
    """Return the number of entities matching the generator-expression filter.

    Example::

        n = count(p for p in Product if p.price > 100)

    Equivalent to::

        db.select(Product).filter(Product.price > 100).count()

    When *gen* does not iterate over an :class:`~nextorm.entity.Entity` class,
    falls back to counting items in the plain Python generator.
    """
    if not _is_entity_generator(gen):
        import builtins  # noqa: PLC0415

        return builtins.sum(1 for _ in gen)
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
    if not _is_entity_generator(gen):
        raise TypeError(
            "avg() requires a generator iterating over an Entity class, "
            "e.g. avg(p.price for p in Product). "
            f"Got: {type(gen.gi_frame.f_locals.get('.0') if gen.gi_frame else None)!r}"
        )
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

    Falls back to ``builtins.sum`` for plain Python generators that do not
    iterate over an :class:`~nextorm.entity.Entity` class.

    Raises
    ------
    DecompileError
        If the generator yields the entity rather than a field attribute.
    """
    assert isinstance(gen, types.GeneratorType)
    if not _is_entity_generator(gen):
        import builtins  # noqa: PLC0415

        return builtins.sum(gen)
    field_name = _decompile_yield_attr(gen.gi_code)
    if field_name is None:
        raise DecompileError(
            "sum() requires a generator that yields a field attribute, "
            "e.g. sum(p.price for p in Product).  "
            "To count rows use count(p for p in Product)."
        )
    qs = select(gen)
    return qs.sum(field_name)


def min(gen: Generator[Any, None, None]) -> Any:
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
    if not _is_entity_generator(gen):
        import builtins  # noqa: PLC0415

        return builtins.min(gen)
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
    if not _is_entity_generator(gen):
        import builtins  # noqa: PLC0415

        return builtins.max(gen)
    field_name = _decompile_yield_attr(gen.gi_code)
    if field_name is None:
        raise DecompileError(
            "max() requires a generator that yields a field attribute, "
            "e.g. max(p.price for p in Product).  "
            "To count rows use count(p for p in Product)."
        )
    qs = select(gen)
    return qs.max(field_name)
