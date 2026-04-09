"""Tests for nextorm.debug — set_sql_debug, sql_debugging, QueryStat, qs.show()."""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING

from nextorm import (
    Database,
    Entity,
    QueryStat,
    Req,
    clear_global_stats,
    global_stats,
    set_sql_debug,
    sql_debugging,
)
from nextorm.async_database import AsyncDatabase
from nextorm.debug import _print_sql

if TYPE_CHECKING:
    import pytest

# ---------------------------------------------------------------------------
# Entity shared across tests
# ---------------------------------------------------------------------------


class Widget(Entity):
    name: Req[str]
    value: Req[int]


def _make_db() -> Database:
    db = Database(entities=[Widget])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    return db


async def _make_async_db() -> AsyncDatabase:
    db = AsyncDatabase(entities=[Widget])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)
    return db


# ---------------------------------------------------------------------------
# set_sql_debug / sql_debugging
# ---------------------------------------------------------------------------


def test_set_sql_debug_enables_output(capsys: pytest.CaptureFixture[str]) -> None:
    db = _make_db()
    set_sql_debug(True)
    try:
        db.select(Widget).fetch_all()
        out = capsys.readouterr().out
        assert ">>>" in out
    finally:
        set_sql_debug(False)


def test_set_sql_debug_disable_suppresses_output(capsys: pytest.CaptureFixture[str]) -> None:
    db = _make_db()
    set_sql_debug(False)
    db.select(Widget).fetch_all()
    out = capsys.readouterr().out
    assert out == ""


def test_sql_debugging_context_manager(capsys: pytest.CaptureFixture[str]) -> None:
    db = _make_db()
    # Before context: no output
    db.select(Widget).fetch_all()
    assert capsys.readouterr().out == ""
    # Inside context: output produced
    with sql_debugging():
        db.select(Widget).fetch_all()
    assert ">>>" in capsys.readouterr().out
    # After context: no output again
    db.select(Widget).fetch_all()
    assert capsys.readouterr().out == ""


def test_sql_debugging_restores_previous_true_state(capsys: pytest.CaptureFixture[str]) -> None:
    """sql_debugging restores the pre-context True state on exit."""
    set_sql_debug(True)
    try:
        with sql_debugging():
            pass  # already True; entering sets True again
        # after exit the previous True is restored
        db = _make_db()
        db.select(Widget).fetch_all()
        assert ">>>" in capsys.readouterr().out
    finally:
        set_sql_debug(False)


def test_print_sql_with_params(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_sql prints params line when params are non-empty."""
    f = io.StringIO()
    set_sql_debug(True)
    try:
        _print_sql("SELECT 1", [42], file=f)
        out = f.getvalue()
        assert "SELECT 1" in out
        assert "42" in out
    finally:
        set_sql_debug(False)


def test_print_sql_no_params(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_sql omits params line when params are empty."""
    f = io.StringIO()
    set_sql_debug(True)
    try:
        _print_sql("SELECT 1", [], file=f)
        assert "params" not in f.getvalue()
    finally:
        set_sql_debug(False)


def test_print_sql_suppressed_when_debug_off() -> None:
    f = io.StringIO()
    set_sql_debug(False)
    _print_sql("SELECT 1", [1], file=f)
    assert f.getvalue() == ""


def test_print_sql_default_file(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_sql writes to sys.stdout by default."""
    set_sql_debug(True)
    try:
        _print_sql("SELECT 42", [])
        assert "SELECT 42" in capsys.readouterr().out
    finally:
        set_sql_debug(False)


# ---------------------------------------------------------------------------
# qs.show() — sync
# ---------------------------------------------------------------------------


def test_show_prints_table() -> None:
    db = _make_db()
    w = Widget(name="sprocket", value=7)
    db.save(w)
    f = io.StringIO()
    db.select(Widget).show(file=f)
    out = f.getvalue()
    assert "name" in out
    assert "sprocket" in out
    assert "value" in out
    assert "7" in out
    assert "+" in out


def test_show_no_results() -> None:
    db = _make_db()
    f = io.StringIO()
    db.select(Widget).show(file=f)
    assert "(no results)" in f.getvalue()


def test_show_default_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    db = _make_db()
    db.select(Widget).show()
    assert "(no results)" in capsys.readouterr().out


def test_show_width_truncates_content() -> None:
    db = _make_db()
    w = Widget(name="x" * 200, value=1)
    db.save(w)
    f = io.StringIO()
    db.select(Widget).show(width=30, file=f)
    lines = [line for line in f.getvalue().splitlines() if line.startswith("|")]
    # Every data/header line must be no wider than 30 chars
    for line in lines:
        assert len(line) <= 30


def test_show_multiple_rows() -> None:
    db = _make_db()
    for i in range(3):
        db.save(Widget(name=f"w{i}", value=i))
    f = io.StringIO()
    db.select(Widget).show(file=f)
    out = f.getvalue()
    for i in range(3):
        assert f"w{i}" in out


# ---------------------------------------------------------------------------
# AsyncQuerySet.ashow()
# ---------------------------------------------------------------------------


def test_ashow_prints_table() -> None:
    async def _run() -> str:
        db = await _make_async_db()
        try:
            await db.asave(Widget(name="gear", value=3))
            f = io.StringIO()
            await db.aselect(Widget).ashow(file=f)
            return f.getvalue()
        finally:
            await db.close()

    out = asyncio.run(_run())
    assert "gear" in out
    assert "3" in out


def test_ashow_no_results() -> None:
    async def _run() -> str:
        db = await _make_async_db()
        try:
            f = io.StringIO()
            await db.aselect(Widget).ashow(file=f)
            return f.getvalue()
        finally:
            await db.close()

    assert "(no results)" in asyncio.run(_run())


def test_ashow_width_truncates() -> None:
    async def _run() -> str:
        db = await _make_async_db()
        try:
            await db.asave(Widget(name="z" * 200, value=99))
            f = io.StringIO()
            await db.aselect(Widget).ashow(width=25, file=f)
            return f.getvalue()
        finally:
            await db.close()

    out = asyncio.run(_run())
    for line in [ln for ln in out.splitlines() if ln.startswith("|")]:
        assert len(line) <= 25


# ---------------------------------------------------------------------------
# local_stats and global_stats
# ---------------------------------------------------------------------------


def test_local_stats_tracks_queries() -> None:
    db = _make_db()
    db.clear_local_stats()
    db.select(Widget).fetch_all()
    stats = db.local_stats
    assert len(stats) == 1
    sql, stat = next(iter(stats.items()))
    assert "SELECT" in sql.upper()
    assert stat.count == 1
    assert stat.sum_time >= 0.0
    assert stat.min_time <= stat.max_time
    assert stat.avg_time >= 0.0


def test_local_stats_accumulates_per_sql() -> None:
    db = _make_db()
    db.clear_local_stats()
    for _ in range(3):
        db.select(Widget).fetch_all()
    sql = next(iter(db.local_stats))
    assert db.local_stats[sql].count == 3


def test_clear_local_stats() -> None:
    db = _make_db()
    db.select(Widget).fetch_all()
    db.clear_local_stats()
    assert db.local_stats == {}


def test_merge_local_stats_into_global() -> None:
    clear_global_stats()
    db = _make_db()
    db.clear_local_stats()
    db.select(Widget).fetch_all()
    db.merge_local_stats()
    assert len(global_stats) >= 1
    stat = next(iter(global_stats.values()))
    assert stat.count >= 1
    clear_global_stats()


def test_merge_local_stats_accumulates() -> None:
    clear_global_stats()
    db = _make_db()
    db.clear_local_stats()
    db.select(Widget).fetch_all()
    db.merge_local_stats()
    db.clear_local_stats()
    db.select(Widget).fetch_all()
    db.merge_local_stats()
    stat = next(iter(global_stats.values()))
    assert stat.count == 2
    clear_global_stats()


def test_clear_global_stats() -> None:
    db = _make_db()
    db.select(Widget).fetch_all()
    db.merge_local_stats()
    clear_global_stats()
    assert global_stats == {}


def test_query_stat_avg_time_zero_count() -> None:
    stat = QueryStat()
    assert stat.avg_time == 0.0


def test_query_stat_merge_empty() -> None:
    stat = QueryStat(count=2, sum_time=1.0, min_time=0.4, max_time=0.6)
    empty = QueryStat()
    stat._merge(empty)
    assert stat.count == 2


def test_query_stat_merge_improves_min() -> None:
    base = QueryStat(count=1, sum_time=0.5, min_time=0.5, max_time=0.5)
    other = QueryStat()
    other._record(0.1)
    base._merge(other)
    assert base.min_time == 0.1
    assert base.max_time == 0.5
    assert base.count == 2


def test_query_stat_merge_improves_max() -> None:
    base = QueryStat(count=1, sum_time=0.1, min_time=0.1, max_time=0.1)
    other = QueryStat()
    other._record(0.9)
    base._merge(other)
    assert base.max_time == 0.9


def test_dml_stats_tracked() -> None:
    """INSERT / UPDATE / DELETE are timed and added to local_stats."""
    db = _make_db()
    db.clear_local_stats()
    w = Widget(name="bolt", value=1)
    db.save(w)
    assert len(db.local_stats) >= 1


# ---------------------------------------------------------------------------
# Async local_stats
# ---------------------------------------------------------------------------


def test_async_local_stats() -> None:
    async def _run() -> dict[str, QueryStat]:
        db = await _make_async_db()
        try:
            db.clear_local_stats()
            await db.aselect(Widget).fetch_all()
            return db.local_stats
        finally:
            await db.close()

    stats = asyncio.run(_run())
    assert len(stats) >= 1
    stat = next(iter(stats.values()))
    assert stat.count == 1
    assert stat.sum_time >= 0.0


def test_async_merge_local_stats() -> None:
    async def _run() -> None:
        clear_global_stats()
        db = await _make_async_db()
        try:
            db.clear_local_stats()
            await db.aselect(Widget).fetch_all()
            db.merge_local_stats()
        finally:
            await db.close()

    asyncio.run(_run())
    assert len(global_stats) >= 1
    clear_global_stats()


def test_async_merge_local_stats_existing_key() -> None:
    """Merging twice accumulates count in the already-present global_stats entry."""

    async def _run() -> None:
        clear_global_stats()
        db = await _make_async_db()
        try:
            db.clear_local_stats()
            await db.aselect(Widget).fetch_all()
            # First merge populates global_stats
            db.merge_local_stats()
            # Second merge hits the 'sql already in global_stats' branch
            db.merge_local_stats()
        finally:
            await db.close()

    asyncio.run(_run())
    stat = next(iter(global_stats.values()))
    assert stat.count == 2
    clear_global_stats()


def test_async_clear_local_stats() -> None:
    async def _run() -> dict[str, QueryStat]:
        db = await _make_async_db()
        try:
            await db.aselect(Widget).fetch_all()
            db.clear_local_stats()
            return db.local_stats
        finally:
            await db.close()

    assert asyncio.run(_run()) == {}


def test_async_dml_stats() -> None:
    async def _run() -> dict[str, QueryStat]:
        db = await _make_async_db()
        try:
            db.clear_local_stats()
            await db.asave(Widget(name="cam", value=5))
            return db.local_stats
        finally:
            await db.close()

    stats = asyncio.run(_run())
    assert len(stats) >= 1


# ---------------------------------------------------------------------------
# show() with no-columns entity (edge case)
# ---------------------------------------------------------------------------


def test_show_no_columns(capsys: pytest.CaptureFixture[str]) -> None:
    """Entity where every field is PK (no non-lazy non-pk fields to display)."""

    class _NoTextCols(Entity):
        pass  # only auto PK

    db2 = Database(entities=[_NoTextCols])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)

    # _NoTextCols has only the auto 'id' PK column — but 'id' is a field
    # so show() will render it.  To exercise the no-columns branch we
    # monkeypatch the entity's _fields_ dict temporarily.
    real_fields = _NoTextCols._fields_
    _NoTextCols._fields_ = {}
    try:
        f = io.StringIO()
        db2.select(_NoTextCols).show(file=f)
        assert "(no columns)" in f.getvalue()
    finally:
        _NoTextCols._fields_ = real_fields


def test_ashow_no_columns() -> None:
    """Same no-columns branch via ashow."""

    class _NoTextColsAsync(Entity):
        pass

    async def _run() -> str:
        db = AsyncDatabase(entities=[_NoTextColsAsync])
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        try:
            real_fields = _NoTextColsAsync._fields_
            _NoTextColsAsync._fields_ = {}
            try:
                f = io.StringIO()
                await db.aselect(_NoTextColsAsync).ashow(file=f)
                return f.getvalue()
            finally:
                _NoTextColsAsync._fields_ = real_fields
        finally:
            await db.close()

    assert "(no columns)" in asyncio.run(_run())
