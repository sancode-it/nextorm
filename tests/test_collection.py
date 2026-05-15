"""Tests for RelatedCollection — O2M and M2M operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from nextorm.collection import RelatedCollection
from nextorm.database import Database
from nextorm.entity import Entity
from nextorm.fields import Req, Set, Single
from nextorm.session import db_session

if TYPE_CHECKING:
    from collections.abc import Generator

# ---------------------------------------------------------------------------
# O2M entities: Post → Comment
# ---------------------------------------------------------------------------


class ColPost(Entity):
    title: Req[str]
    comments: Set["ColComment"]  # noqa: UP037


class ColComment(Entity):
    text: Req[str]
    post: Single[ColPost | None]  # nullable for remove/clear tests


# ---------------------------------------------------------------------------
# M2M entities: Article ↔ Tag
# ---------------------------------------------------------------------------


class ColArticle(Entity):
    title: Req[str]
    tags: Set["ColTag"]  # noqa: UP037


class ColTag(Entity):
    label: Req[str]
    articles: Set[ColArticle]


# ---------------------------------------------------------------------------
# No-backref entities for prefetch edge-case testing
# ---------------------------------------------------------------------------


class _NoBRChild(Entity):
    """Child entity with NO ManyToOne pointing back to any owner."""

    value: Req[str]


class _NoBROwner(Entity):
    """Owner entity whose Set target has no ManyToOne back-ref."""

    label: Req[str]
    children: Set[_NoBRChild]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def o2m_db() -> Generator[Database, None, None]:
    db = Database(entities=[ColPost, ColComment])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    yield db
    db.close()


@pytest.fixture
def m2m_db() -> Generator[Database, None, None]:
    db = Database(entities=[ColArticle, ColTag])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    yield db
    db.close()


# ---------------------------------------------------------------------------
# _require_db raises when db is None
# ---------------------------------------------------------------------------


def test_collection_require_db_raises_without_db() -> None:
    post = ColPost.__new__(ColPost)
    vars(post)["_field_id"] = 1
    col: RelatedCollection[Any] = RelatedCollection(post, ColPost._relations_["comments"], None)
    with pytest.raises(RuntimeError, match="database context"):
        col.count()


# ---------------------------------------------------------------------------
# O2M: basic CRUD
# ---------------------------------------------------------------------------


def test_o2m_count_empty(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="A")
    assert post.comments.count() == 0


def test_o2m_is_empty_true(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="B")
    assert post.comments.is_empty() is True


def test_o2m_add_comment(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P1")
    c = ColComment(text="hello")
    post.comments.add(c)
    assert post.comments.count() == 1


def test_o2m_iter_comments(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P2")
    c = ColComment(text="first")
    post.comments.add(c)
    items = list(post.comments)
    assert len(items) == 1
    assert items[0].text == "first"


def test_o2m_len(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P3")
    for i in range(3):
        post.comments.add(ColComment(text=f"c{i}"))
    assert len(post.comments) == 3


def test_o2m_contains_true(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P4")
    c = ColComment(text="hi")
    post.comments.add(c)
    # After add, c.post is set to post → FK is stored in c.__dict__
    loaded = post.comments.load()
    assert loaded[0] in post.comments


def test_o2m_contains_false_when_not_added(o2m_db: Database) -> None:
    with db_session:
        p1 = ColPost(title="P5a")
        p2 = ColPost(title="P5b")
    c = ColComment(text="other")
    p1.comments.add(c)
    # c should NOT be in p2's comments
    in_p2 = any(True for _ in p2.comments)
    assert not in_p2


def test_o2m_contains_wrong_type_returns_false(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P6")
    assert (post not in post.comments) is True


def test_o2m_copy(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P7")
    c1 = ColComment(text="a")
    c2 = ColComment(text="b")
    post.comments.add(c1, c2)
    s = post.comments.copy()
    assert isinstance(s, set)
    assert len(s) == 2


def test_o2m_load_returns_list(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P8")
    c = ColComment(text="loadme")
    post.comments.add(c)
    result: list[Any] = post.comments.load()
    assert isinstance(result, list)
    assert len(result) == 1


def test_o2m_filter_no_args_returns_queryset(o2m_db: Database) -> None:
    from nextorm.query import QuerySet

    with db_session:
        post = ColPost(title="P9")
    qs = post.comments.filter()
    assert isinstance(qs, QuerySet)


def test_o2m_select_returns_queryset(o2m_db: Database) -> None:
    """select() with no args returns an unfiltered QuerySet for the whole collection."""
    from nextorm.query import QuerySet

    with db_session:
        post = ColPost(title="P9b")
    post.comments.add(ColComment(text="x"))
    post.comments.add(ColComment(text="y"))
    qs = post.comments.select()
    assert isinstance(qs, QuerySet)
    assert qs.count() == 2


def test_o2m_filter_returns_queryset(o2m_db: Database) -> None:
    from nextorm.query import QuerySet
    from nextorm.sql.nodes import BinOp, ColumnRef, Param

    with db_session:
        post = ColPost(title="P10")
    c = ColComment(text="filtered")
    post.comments.add(c)
    qs = post.comments.filter(BinOp(ColumnRef("text"), "=", Param(value="filtered")))
    assert isinstance(qs, QuerySet)
    assert qs.count() == 1


def test_o2m_remove_comment(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P11")
    c = ColComment(text="removeme")
    post.comments.add(c)
    assert post.comments.count() == 1

    # Reload the comment to have proper id
    loaded = post.comments.load()[0]
    post.comments.remove(loaded)
    assert post.comments.count() == 0


def test_o2m_clear(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P12")
    for i in range(2):
        post.comments.add(ColComment(text=f"del{i}"))
    assert post.comments.count() == 2
    post.comments.clear()
    assert post.comments.count() == 0


def test_o2m_create_and_link(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P13")
    c = post.comments.create(text="auto")
    assert isinstance(c, ColComment)
    assert post.comments.count() == 1


def test_o2m_remove_without_nullable_raises(o2m_db: Database) -> None:
    """remove() on O2M where FK is not nullable should raise RuntimeError."""
    # ColComment.post is nullable, so this works fine in this suite.
    # To test the error, let's set up a scenario directly on the collection.
    db = o2m_db
    with db_session:
        post = ColPost(title="P14")
    c = ColComment(text="test")
    post.comments.add(c)

    # Create a collection that points at a fake target with no nullable ManyToOne back-ref
    from nextorm.entity import RelationInfo
    from nextorm.fields import RelationKind, RelationSpec

    class _NNN(Entity):
        x: Req[str]

    # Craft a RelationInfo pointing at _NNN (no back-ref)
    ri = RelationInfo("nnn", RelationSpec(kind=RelationKind.SET, target=_NNN))
    col: RelatedCollection[Any] = RelatedCollection(post, ri, db)
    # Patching _is_m2m to return False and _resolve_target to return _NNN
    col._is_m2m = lambda: False  # type: ignore[method-assign]
    col._resolve_target = lambda: _NNN  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="back-reference"):
        col.remove(ColComment.__new__(ColComment))


def test_o2m_clear_without_nullable_raises(o2m_db: Database) -> None:
    db = o2m_db
    with db_session:
        post = ColPost(title="P15")

    from nextorm.entity import RelationInfo
    from nextorm.fields import RelationKind, RelationSpec

    class _NNN2(Entity):
        y: Req[str]

    ri = RelationInfo("nnn2", RelationSpec(kind=RelationKind.SET, target=_NNN2))
    col: RelatedCollection[Any] = RelatedCollection(post, ri, db)
    col._is_m2m = lambda: False  # type: ignore[method-assign]
    col._resolve_target = lambda: _NNN2  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="back-reference"):
        col.clear()


def test_o2m_no_backref_raises_on_add(o2m_db: Database) -> None:
    db = o2m_db
    with db_session:
        post = ColPost(title="P16")

    from nextorm.entity import RelationInfo
    from nextorm.fields import RelationKind, RelationSpec

    class _NNB(Entity):
        z: Req[str]

    ri = RelationInfo("nnb", RelationSpec(kind=RelationKind.SET, target=_NNB))
    col: RelatedCollection[Any] = RelatedCollection(post, ri, db)
    col._is_m2m = lambda: False  # type: ignore[method-assign]
    col._resolve_target = lambda: _NNB  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="back-reference"):
        col.add(_NNB.__new__(_NNB))


def test_o2m_no_backref_build_queryset_raises(o2m_db: Database) -> None:
    """_build_queryset raises when no ManyToOne back-ref exists on target."""
    db = o2m_db
    with db_session:
        post = ColPost(title="P17")

    from nextorm.entity import RelationInfo
    from nextorm.fields import RelationKind, RelationSpec

    # Use ColPost as both owner and target — ColPost has no ManyToOne pointing at itself,
    # so the back-ref check will fail. ColPost IS in the schema so db.select() succeeds.
    ri = RelationInfo("self_ref", RelationSpec(kind=RelationKind.SET, target=ColPost))
    col: RelatedCollection[Any] = RelatedCollection(post, ri, db)
    col._is_m2m = lambda: False  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="back-reference"):
        col.count()


def test_o2m_is_empty_false_when_not_empty(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P18")
    post.comments.add(ColComment(text="x"))
    assert post.comments.is_empty() is False


def test_o2m_repr(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P19")
    r = repr(post.comments)
    assert "RelatedCollection" in r
    assert "colcomment" in r.lower() or "ColComment" in r


def test_o2m_cache_invalidated_after_add(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="P20")
    # load once (empty)
    first: list[Any] = post.comments.load()
    assert len(first) == 0
    # add a comment
    post.comments.add(ColComment(text="new"))
    # cache was invalidated, next load should return updated list
    second: list[Any] = post.comments.load()
    assert len(second) == 1


# ---------------------------------------------------------------------------
# M2M: basic CRUD
# ---------------------------------------------------------------------------


def test_m2m_count_empty(m2m_db: Database) -> None:
    with db_session:
        art = ColArticle(title="Art1")
    assert art.tags.count() == 0


def test_m2m_is_empty_true(m2m_db: Database) -> None:
    with db_session:
        art = ColArticle(title="Art2")
    assert art.tags.is_empty() is True


def test_m2m_add_tag(m2m_db: Database) -> None:
    with db_session:
        art = ColArticle(title="Art3")
        tag = ColTag(label="python")
    art.tags.add(tag)
    assert art.tags.count() == 1


def test_m2m_iter_tags(m2m_db: Database) -> None:
    with db_session:
        art = ColArticle(title="Art4")
        tag = ColTag(label="tech")
    art.tags.add(tag)
    items = list(art.tags)
    assert len(items) == 1
    assert items[0].label == "tech"


def test_m2m_contains_true(m2m_db: Database) -> None:
    with db_session:
        art = ColArticle(title="Art5")
        tag = ColTag(label="news")
    art.tags.add(tag)
    assert tag in art.tags


def test_m2m_contains_false(m2m_db: Database) -> None:
    with db_session:
        art = ColArticle(title="Art6")
        tag_in = ColTag(label="yes")
        tag_out = ColTag(label="no")
    art.tags.add(tag_in)
    assert tag_out not in art.tags


def test_m2m_contains_missing_pk_returns_false(m2m_db: Database) -> None:
    with db_session:
        art = ColArticle(title="Art7")
    tag_no_pk = ColTag.__new__(ColTag)  # no id set
    result = tag_no_pk in art.tags
    assert result is False


def test_m2m_remove_tag(m2m_db: Database) -> None:
    with db_session:
        art = ColArticle(title="Art8")
        tag = ColTag(label="rm")
    art.tags.add(tag)
    assert art.tags.count() == 1
    art.tags.remove(tag)
    assert art.tags.count() == 0


def test_m2m_clear(m2m_db: Database) -> None:
    with db_session:
        art = ColArticle(title="Art9")
        tags = [ColTag(label=f"t{i}") for i in range(3)]
    for t in tags:
        art.tags.add(t)
    assert art.tags.count() == 3
    art.tags.clear()
    assert art.tags.count() == 0


def test_m2m_copy_is_set(m2m_db: Database) -> None:
    with db_session:
        art = ColArticle(title="Art10")
        tag = ColTag(label="settest")
    art.tags.add(tag)
    s = art.tags.copy()
    assert isinstance(s, set)
    assert len(s) == 1


def test_m2m_create_and_link(m2m_db: Database) -> None:
    with db_session:
        art = ColArticle(title="Art11")
    tag = art.tags.create(label="auto-created")
    assert isinstance(tag, ColTag)
    assert art.tags.count() == 1


# ---------------------------------------------------------------------------
# Unresolvable target
# ---------------------------------------------------------------------------


def test_resolve_target_unresolvable_raises() -> None:
    from nextorm.entity import RelationInfo
    from nextorm.fields import RelationKind, RelationSpec

    class _Stub(Entity):
        pass

    ri = RelationInfo("broken", RelationSpec(kind=RelationKind.SET, target="DoesNotExistXYZ"))
    col: RelatedCollection[Any] = RelatedCollection(_Stub.__new__(_Stub), ri, None)
    with pytest.raises(RuntimeError, match="resolve"):
        col._resolve_target()


# ---------------------------------------------------------------------------
# QuerySet.prefetch() — O2M
# ---------------------------------------------------------------------------


def test_prefetch_o2m_empty_results(o2m_db: Database) -> None:
    """_do_prefetch is a no-op when the query returns no rows."""
    db = o2m_db
    posts = db.select(ColPost).prefetch("comments").fetch_all()
    assert posts == []


def test_prefetch_o2m_populates_cache(o2m_db: Database) -> None:
    """prefetch('comments') attaches a pre-filled RelatedCollection."""
    db = o2m_db
    with db_session:
        post = ColPost(title="Pf1")
    post.comments.add(ColComment(text="a"))
    post.comments.add(ColComment(text="b"))

    posts = db.select(ColPost).prefetch("comments").fetch_all()
    assert len(posts) == 1
    col: RelatedCollection[ColComment] = posts[0].__dict__["_comments_col"]
    assert isinstance(col, RelatedCollection)
    assert col._cache is not None
    assert len(col._cache) == 2


def test_prefetch_o2m_empty_collection_cached(o2m_db: Database) -> None:
    """A post with no comments still gets an empty cached RelatedCollection."""
    db = o2m_db
    with db_session:
        ColPost(title="Pf-empty")

    posts = db.select(ColPost).prefetch("comments").fetch_all()
    col: RelatedCollection[ColComment] | None = posts[0].__dict__.get("_comments_col")
    assert col is not None
    assert col._cache == []


def test_prefetch_o2m_multiple_owners(o2m_db: Database) -> None:
    """prefetch distributes comments to the right owner posts."""
    db = o2m_db
    with db_session:
        p1 = ColPost(title="P-a")
        p2 = ColPost(title="P-b")
    p1.comments.add(ColComment(text="c1"))
    p2.comments.add(ColComment(text="c2"))
    p2.comments.add(ColComment(text="c3"))

    posts = db.select(ColPost).prefetch("comments").fetch_all()
    by_title = {p.title: p for p in posts}
    assert len(by_title["P-a"].__dict__["_comments_col"]._cache) == 1
    assert len(by_title["P-b"].__dict__["_comments_col"]._cache) == 2


# ---------------------------------------------------------------------------
# QuerySet.prefetch() — M2M
# ---------------------------------------------------------------------------


def test_prefetch_m2m_populates_cache(m2m_db: Database) -> None:
    """prefetch('tags') attaches a pre-filled RelatedCollection for M2M."""
    db = m2m_db
    with db_session:
        art = ColArticle(title="Art-pf")
        tag = ColTag(label="python")
    art.tags.add(tag)

    articles = db.select(ColArticle).prefetch("tags").fetch_all()
    col = articles[0].__dict__["_tags_col"]
    assert len(col._cache) == 1
    assert col._cache[0].label == "python"


def test_prefetch_m2m_empty_collection_cached(m2m_db: Database) -> None:
    """An article with no tags still gets an empty cached RelatedCollection."""
    db = m2m_db
    with db_session:
        ColArticle(title="Art-empty")

    articles = db.select(ColArticle).prefetch("tags").fetch_all()
    col = articles[0].__dict__.get("_tags_col")
    assert col is not None
    assert col._cache == []


def test_prefetch_manytomany_skips_manytone_field() -> None:
    """prefetch() silently skips relations on a ManyToOne field (no-op, line 486)."""
    # ColComment.post is ManyToOne — the prefetch silently continues
    db = Database(entities=[ColPost, ColComment])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    with db_session:
        post = ColPost(title="PR")
    c = ColComment(text="pr_c")
    post.comments.add(c)

    comments = db.select(ColComment).prefetch("post").fetch_all()
    # ManyToOne prefetch is a no-op — no crash, no extra attribute set
    assert len(comments) == 1
    db.close()


def test_prefetch_o2m_no_backref_continues() -> None:
    """O2M prefetch silently skips when target has no ManyToOne back-ref (line 568)."""
    db = Database(entities=[_NoBROwner, _NoBRChild])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)

    with db_session:
        _NoBROwner(label="o")

    owners = db.select(_NoBROwner).prefetch("children").fetch_all()
    # No back-ref → _do_prefetch skips → no _children_col dict entry
    assert "_children_col" not in owners[0].__dict__
    db.close()


def test_ensure_loaded_uses_cache_on_second_call(o2m_db: Database) -> None:
    """Second call to load() reuses cache (175->177 branch taken when cache is warm)."""
    with db_session:
        post = ColPost(title="CacheBranch")
    first = post.comments.load()
    # No mutation — _cache is still set
    second = post.comments.load()
    assert first is second  # same list object returned from cache


def test_o2m_contains_pk_field_none_returns_false() -> None:
    """__contains__ returns False when target entity has no PK field (line 199)."""

    class _PKlessChild(Entity):
        """Entity without auto-pk — we'll fake it by clearing _pk_field_ at runtime."""

        label: Req[str]

    # Manually clear _pk_field_ to simulate a PK-less entity
    _PKlessChild._pk_field_ = None

    owner = ColPost.__new__(ColPost)
    vars(owner)["_field_id"] = 1

    from nextorm.entity import RelationInfo  # noqa: PLC0415
    from nextorm.fields import RelationKind, RelationSpec  # noqa: PLC0415

    fake_ri = RelationInfo("comments", RelationSpec(kind=RelationKind.SET, target=_PKlessChild))
    col: RelatedCollection[Any] = RelatedCollection(owner, fake_ri, None)
    child = _PKlessChild.__new__(_PKlessChild)
    result = child in col  # triggers __contains__ → pk_field is None → return False
    assert result is False


def test_o2m_contains_stored_none_returns_false(o2m_db: Database) -> None:
    """__contains__ returns False when item has no FK stored (line 257)."""
    with db_session:
        post = ColPost(title="ContStored")
    # Create a comment with an id but without a post FK — _post_id and _post_obj absent
    c = ColComment.__new__(ColComment)
    vars(c)["_field_id"] = 99  # set id using internal storage key
    # _post_id and _post_obj are absent from __dict__
    result = c in post.comments
    assert result is False


def test_o2m_contains_stored_object_returns_true(o2m_db: Database) -> None:
    """__contains__ stored as object (not int) hits lines 260-262."""
    with db_session:
        post = ColPost(title="ContObj")
    c = ColComment(text="objfk")
    post.comments.add(c)
    # Manually replace _post_id with the actual post object to hit the object path
    vars(c)["_post_id"] = None  # clear int FK
    vars(c)["_post_obj"] = post  # store object instead
    # Ensure item has a valid pk so we get to the FK check
    assert c.id is not None  # sanity
    result = c in post.comments
    assert result is True


def test_o2m_contains_no_backref_returns_false() -> None:
    """__contains__ returns False when no ManyToOne backref found on target (line 251)."""
    from nextorm.entity import RelationInfo  # noqa: PLC0415
    from nextorm.fields import RelationKind, RelationSpec  # noqa: PLC0415

    owner = ColPost.__new__(ColPost)
    vars(owner)["_field_id"] = 1
    # Use _NoBRChild as target — it has no ManyToOne back-ref pointing to ColPost
    fake_ri = RelationInfo("children", RelationSpec(kind=RelationKind.SET, target=_NoBRChild))
    col: RelatedCollection[Any] = RelatedCollection(owner, fake_ri, None)
    child = _NoBRChild.__new__(_NoBRChild)
    vars(child)["_field_id"] = 5  # set pk using internal storage key
    result = child in col
    assert result is False


def test_o2m_contains_with_explicit_reverse_uses_back_ref_name(o2m_db: Database) -> None:
    """__contains__ with reverse= set skips back-ref discovery (237->253 False branch)."""
    from nextorm.entity import RelationInfo  # noqa: PLC0415
    from nextorm.fields import RelationKind, RelationSpec  # noqa: PLC0415

    db = o2m_db
    with db_session:
        post = ColPost(title="RevPost")
    c = ColComment(text="rev")
    post.comments.add(c)

    # Build a collection with reverse="post" explicitly set
    owner = post
    explicit_ri = RelationInfo(
        "comments",
        RelationSpec(kind=RelationKind.SET, target=ColComment, reverse="post"),
    )
    col: RelatedCollection[Any] = RelatedCollection(owner, explicit_ri, db)
    # back_ref_name is already "post" → skips auto-discovery → goes to br_id check
    assert c in col


# ---------------------------------------------------------------------------
# Collection.order_by / page / random
# ---------------------------------------------------------------------------


def test_o2m_order_by_returns_queryset(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="Ordered")
    for text in ("z first", "a second", "m third"):
        post.comments.add(ColComment(text=text))
    ordered = post.comments.order_by(ColComment.text.asc()).fetch_all()
    assert [c.text for c in ordered] == ["a second", "m third", "z first"]


def test_o2m_page_returns_correct_slice(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="Paged")
    for i in range(5):
        post.comments.add(ColComment(text=f"c{i}"))
    page1 = post.comments.order_by(ColComment.id.asc()).page(1, 3).fetch_all()
    assert len(page1) == 3
    page2 = post.comments.order_by(ColComment.id.asc()).page(2, 3).fetch_all()
    assert len(page2) == 2


def test_o2m_collection_page_direct(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="DirectPage")
    for i in range(5):
        post.comments.add(ColComment(text=f"p{i}"))
    page1 = post.comments.page(1, 3).fetch_all()
    assert len(page1) == 3
    page2 = post.comments.page(2, 3).fetch_all()
    assert len(page2) == 2


def test_o2m_random_returns_n_rows(o2m_db: Database) -> None:
    with db_session:
        post = ColPost(title="Random")
    for i in range(5):
        post.comments.add(ColComment(text=f"r{i}"))
    results = post.comments.random(2).fetch_all()
    assert len(results) == 2


def test_o2m_remove_multiple_items(o2m_db: Database) -> None:
    """Test remove() with a list of items."""
    with db_session:
        post = ColPost(title="RemoveMulti")
    comments = [ColComment(text=f"c{i}") for i in range(3)]
    for c in comments:
        post.comments.add(c)
    assert post.comments.count() == 3

    # Reload comments to have proper ids
    loaded = post.comments.load()
    # Remove 2 items using a list
    post.comments.remove(loaded[:2])
    assert post.comments.count() == 1


def test_o2m_remove_multiple_items_individual_args(o2m_db: Database) -> None:
    """Test remove() with individual item arguments."""
    with db_session:
        post = ColPost(title="RemoveMulti2")
    comments = [ColComment(text=f"c{i}") for i in range(3)]
    for c in comments:
        post.comments.add(c)
    assert post.comments.count() == 3

    # Reload comments to have proper ids
    loaded = post.comments.load()
    # Remove using individual args
    post.comments.remove(loaded[0], loaded[1])
    assert post.comments.count() == 1


def test_m2m_remove_multiple_items(m2m_db: Database) -> None:
    """Test remove() with a list of items on M2M relation."""
    with db_session:
        art = ColArticle(title="ArtMulti")
        tags = [ColTag(label=f"t{i}") for i in range(3)]
    for t in tags:
        art.tags.add(t)
    assert art.tags.count() == 3

    # Reload tags to have proper ids
    loaded = art.tags.load()
    # Remove 2 items using a list
    art.tags.remove(loaded[:2])
    assert art.tags.count() == 1


def test_m2m_remove_multiple_items_individual_args(m2m_db: Database) -> None:
    """Test remove() with individual item arguments on M2M relation."""
    with db_session:
        art = ColArticle(title="ArtMulti2")
        tags = [ColTag(label=f"t{i}") for i in range(3)]
    for t in tags:
        art.tags.add(t)
    assert art.tags.count() == 3

    # Reload tags to have proper ids
    loaded = art.tags.load()
    # Remove using individual args
    art.tags.remove(loaded[0], loaded[1])
    assert art.tags.count() == 1


# ---------------------------------------------------------------------------
# RelatedCollection.drop_table() on a non-M2M relation raises RuntimeError
# ---------------------------------------------------------------------------


def test_o2m_collection_drop_table_raises(o2m_db: Database) -> None:
    """drop_table() on a one-to-many collection raises RuntimeError (lines 509-517)."""
    with db_session:
        post = ColPost(title="DT Test")
    collection = post.comments
    with pytest.raises(RuntimeError, match="only valid for many-to-many"):
        collection.drop_table(with_all_data=True)


def test_m2m_collection_drop_table(m2m_db: Database) -> None:
    """drop_table() on a M2M collection drops the join table (lines 515-517)."""
    with db_session:
        art = ColArticle(title="Drop M2M Test")
        tag = ColTag(label="test-tag-drop")
    art.tags.add(tag)
    assert art.tags.count() == 1
    art.tags.drop_table(with_all_data=True)


# ---------------------------------------------------------------------------
# RelatedCollection.where() and filter(**kwargs)
# ---------------------------------------------------------------------------


def test_collection_select_with_predicate(o2m_db: Database) -> None:
    """select(lambda c: ...) pre-filters collection via lambda predicate."""
    with db_session:
        post = ColPost(title="Select Pred")
    post.comments.add(ColComment(text="yes"))
    post.comments.add(ColComment(text="no"))
    results = post.comments.select(  # pyright: ignore[reportUnknownLambdaType,reportUnknownMemberType]
        lambda c: c.text == "yes"  # pyright: ignore[reportUnknownLambdaType,reportUnknownMemberType]
    ).fetch_all()
    assert len(results) == 1
    assert results[0].text == "yes"


def test_collection_select_with_conditions(o2m_db: Database) -> None:
    """select(SqlNode) pre-filters collection via positional condition."""
    from nextorm.sql.nodes import BinOp, ColumnRef, Param

    with db_session:
        post = ColPost(title="Select Cond")
    post.comments.add(ColComment(text="match"))
    post.comments.add(ColComment(text="other"))
    results = post.comments.select(
        None, BinOp(ColumnRef("text"), "=", Param(value="match"))
    ).fetch_all()
    assert len(results) == 1
    assert results[0].text == "match"


def test_collection_select_with_kwargs(o2m_db: Database) -> None:
    """select(field=value) pre-filters collection via keyword equality."""
    with db_session:
        post = ColPost(title="Select KW")
    post.comments.add(ColComment(text="alpha"))
    post.comments.add(ColComment(text="beta"))
    results = post.comments.select(text="alpha").fetch_all()
    assert len(results) == 1
    assert results[0].text == "alpha"


def test_collection_select_combined(o2m_db: Database) -> None:
    """select(predicate, condition, **kwargs) combines all three."""
    from nextorm.sql.nodes import BinOp, ColumnRef, Param

    with db_session:
        post = ColPost(title="Select Combined")
    post.comments.add(ColComment(text="yes"))
    post.comments.add(ColComment(text="no"))
    post.comments.add(ColComment(text="maybe"))
    cond = BinOp(ColumnRef("id"), ">", Param(value=0))
    results = post.comments.select(  # pyright: ignore[reportUnknownLambdaType,reportUnknownMemberType]
        lambda c: c.text != "no",  # pyright: ignore[reportUnknownLambdaType,reportUnknownMemberType]
        cond,
        text="yes",
    ).fetch_all()
    assert len(results) == 1
    assert results[0].text == "yes"


def test_collection_where_with_predicate(o2m_db: Database) -> None:
    """where(lambda c: ...) filters collection via lambda predicate."""
    with db_session:
        post = ColPost(title="Where Pred")
    post.comments.add(ColComment(text="yes"))
    post.comments.add(ColComment(text="no"))
    results = post.comments.where(lambda c: c.text == "yes").fetch_all()
    assert len(results) == 1
    assert results[0].text == "yes"


def test_collection_filter_with_kwargs(o2m_db: Database) -> None:
    """filter(text=...) filters collection via keyword equality."""
    with db_session:
        post = ColPost(title="Filter KW")
    post.comments.add(ColComment(text="alpha"))
    post.comments.add(ColComment(text="beta"))
    results = post.comments.filter(text="alpha").fetch_all()
    assert len(results) == 1
    assert results[0].text == "alpha"


# ---------------------------------------------------------------------------
# filter() with callable predicate (lines 394-397)
# ---------------------------------------------------------------------------


def test_collection_where_with_callable_predicate(o2m_db: Database) -> None:
    """where(lambda c: ...) triggers lambda predicate filtering."""
    with db_session:
        post = ColPost(title="Where Lambda")
    post.comments.add(ColComment(text="yes"))
    post.comments.add(ColComment(text="no"))
    results = post.comments.where(  # pyright: ignore[reportUnknownLambdaType,reportUnknownMemberType]
        lambda c: c.text == "yes"  # pyright: ignore[reportUnknownLambdaType,reportUnknownMemberType]
    ).fetch_all()
    assert len(results) == 1
    assert results[0].text == "yes"


# ---------------------------------------------------------------------------
# filter() with extra positional conditions (line 402)
# ---------------------------------------------------------------------------


def test_collection_filter_with_extra_conditions(o2m_db: Database) -> None:
    """filter(cond1, cond2) applies multiple SqlNode conditions."""
    from nextorm.sql.nodes import BinOp, ColumnRef, Param

    with db_session:
        post = ColPost(title="Filter Extra")
    post.comments.add(ColComment(text="alpha"))
    post.comments.add(ColComment(text="beta"))
    cond1 = BinOp(ColumnRef("id"), ">", Param(value=0))
    cond2 = BinOp(ColumnRef("text"), "=", Param(value="alpha"))
    results = post.comments.filter(cond1, cond2).fetch_all()
    assert len(results) == 1
    assert results[0].text == "alpha"


# ---------------------------------------------------------------------------
# remove() non-nullable FK → db.delete_instance (line 548)
# ---------------------------------------------------------------------------


class _RequiredPost(Entity):
    _table_ = "_req_post"
    title: Req[str]
    items: Set["_RequiredItem"]  # noqa: UP037


class _RequiredItem(Entity):
    _table_ = "_req_item"
    text: Req[str]
    post: Single[_RequiredPost]  # NOT nullable → remove() should delete the item


@pytest.fixture
def req_o2m_db() -> Generator[Database, None, None]:
    db = Database(entities=[_RequiredPost, _RequiredItem])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    yield db
    db.close()


def test_o2m_remove_with_required_fk_deletes_item(req_o2m_db: Database) -> None:
    """remove() with a non-nullable FK deletes the item via db.delete_instance (line 548)."""
    with db_session:
        post = _RequiredPost(title="P")
    item = _RequiredItem(text="item")
    post.items.add(item)
    assert post.items.count() == 1
    loaded = post.items.load()[0]
    # Removing from non-nullable FK should delete the item
    post.items.remove(loaded)
    assert post.items.count() == 0


# ---------------------------------------------------------------------------
# _target_pk_col() returns "id" when target has no pk_fields (line 135)
# ---------------------------------------------------------------------------


def test_target_pk_col_returns_id_when_no_pk_fields(o2m_db: Database) -> None:
    """_target_pk_col() returns 'id' when target entity has no _pk_fields_ (line 135)."""
    from nextorm.collection import RelatedCollection
    from nextorm.entity import RelationInfo
    from nextorm.fields import RelationKind, RelationSpec

    with db_session:
        post = ColPost(title="PKless")

    class _FakeTarget:
        _pk_fields_: tuple[()] = ()
        _table_name_ = "fake"

    ri = RelationInfo("items", RelationSpec(kind=RelationKind.SET, target=ColComment))
    col: RelatedCollection[Any] = RelatedCollection(post, ri, o2m_db)
    col._resolve_target = lambda: _FakeTarget  # type: ignore[method-assign,return-value,assignment]
    result = col._target_pk_col()
    assert result == "id"


# ---------------------------------------------------------------------------
# _is_m2m(): reverse_name set but back_ri is None → fallback (line 114→117)
# ---------------------------------------------------------------------------


def test_is_m2m_reverse_name_set_but_back_ri_none_uses_fallback(o2m_db: Database) -> None:
    """_is_m2m() when reverse is set but relation not found on target → fallback (line 114→117)."""
    from nextorm.collection import RelatedCollection
    from nextorm.entity import RelationInfo
    from nextorm.fields import RelationKind, RelationSpec

    with db_session:
        post = ColPost(title="RevTest")

    # Use reverse="nonexistent_name" so back_ri will be None
    ri = RelationInfo(
        "comments",
        RelationSpec(kind=RelationKind.SET, target=ColComment, reverse="nonexistent_name"),
    )
    col: RelatedCollection[Any] = RelatedCollection(post, ri, o2m_db)
    # ColComment has no Set pointing back at ColPost → fallback returns False (O2M)
    result = col._is_m2m()
    assert result is False


# ---------------------------------------------------------------------------
# _build_queryset() composite FK O2M (lines 197-209)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _build_queryset() composite FK O2M (lines 197-209)
# ---------------------------------------------------------------------------


def test_o2m_build_queryset_composite_fk_owner(o2m_db: Database) -> None:
    """O2M _build_queryset with composite-PK owner builds AND conditions (lines 197-209)."""
    from nextorm.collection import RelatedCollection
    from nextorm.entity import RelationInfo
    from nextorm.fields import RelationKind, RelationSpec

    with db_session:
        post = ColPost(title="CompFK")

    post.comments.add(ColComment(text="child"))
    ri = RelationInfo("comments", RelationSpec(kind=RelationKind.SET, target=ColComment))
    col: RelatedCollection[Any] = RelatedCollection(post, ri, o2m_db)
    col._is_m2m = lambda: False  # type: ignore[method-assign]
    # Simulate composite PK: return tuple (id, id) so isinstance(pk, tuple) is True
    real_pk = post.__dict__.get("_field_id") or 1
    col._owner_pk = lambda: (real_pk,)  # type: ignore[method-assign]
    # Triggers _build_queryset() → composite FK branch (1-elem tuple → equal len → no loop body)
    results = col.load()
    assert len(results) == 1


def test_o2m_build_queryset_composite_fk_owner_multi_col(o2m_db: Database) -> None:
    """Composite FK with 2+ columns exercises the inner loop body (line 204)."""
    from nextorm.collection import RelatedCollection
    from nextorm.entity import RelationInfo
    from nextorm.fields import RelationKind, RelationSpec

    with db_session:
        post = ColPost(title="CompFK2")

    post.comments.add(ColComment(text="c"))
    ri = RelationInfo("comments", RelationSpec(kind=RelationKind.SET, target=ColComment))
    col: RelatedCollection[Any] = RelatedCollection(post, ri, o2m_db)
    col._is_m2m = lambda: False  # type: ignore[method-assign]
    real_pk = post.__dict__.get("_field_id") or 1
    # Simulate a 2-element composite PK → triggers the for-loop body in the AND builder
    col._owner_pk = lambda: (real_pk, real_pk)  # type: ignore[method-assign]

    # Also patch _derive_composite_fk_cols to return 2 columns matching the 2-element PK
    import nextorm.entity as _entity_mod  # noqa: PLC0415

    orig_derive = _entity_mod._derive_composite_fk_cols

    def fake_derive(name: str, owner_cls: Any) -> list[str]:
        return ["post_id", "post_id"]

    _entity_mod._derive_composite_fk_cols = fake_derive  # type: ignore[assignment]
    try:
        results = col.load()
    finally:
        _entity_mod._derive_composite_fk_cols = orig_derive
    assert len(results) == 1


def test_o2m_build_queryset_composite_pk_len_mismatch_falls_through(o2m_db: Database) -> None:
    """When composite PK tuple length != FK col count, falls through to simple FK (line 200→212)."""
    from nextorm.collection import RelatedCollection
    from nextorm.entity import RelationInfo
    from nextorm.fields import RelationKind, RelationSpec
    from nextorm.query import QuerySet

    with db_session:
        post = ColPost(title="CompFKMismatch")

    ri = RelationInfo("comments", RelationSpec(kind=RelationKind.SET, target=ColComment))
    col: RelatedCollection[Any] = RelatedCollection(post, ri, o2m_db)
    col._is_m2m = lambda: False  # type: ignore[method-assign]
    real_pk = post.__dict__.get("_field_id") or 1
    # 2-element tuple → len mismatch with 1-element fk_col_names → falls through to simple FK
    col._owner_pk = lambda: (real_pk, real_pk)  # type: ignore[method-assign]
    # Just build the queryset (don't execute) to exercise the fallthrough branch
    qs = col._build_queryset()
    assert isinstance(qs, QuerySet)
