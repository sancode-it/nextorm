"""Tests for schema data structures, builder, diff, and DDL renderers."""

from __future__ import annotations

import datetime
import decimal
import uuid

import pytest

from nextorm import Entity, FieldSpec, Req, Set, Single, composite_index, composite_key
from nextorm.entity import RelationInfo
from nextorm.fields import PK, Json, LongStr, Opt
from nextorm.fields import RelationKind as _RK
from nextorm.fields import RelationSpec as _RS
from nextorm.schema import (
    AddColumn,
    AddIndex,
    AlterColumnType,
    Column,
    CreateTable,
    DDLRenderer,
    DropColumn,
    DropIndex,
    DropTable,
    ForeignKey,
    Index,
    SQLiteRenderer,
    Table,
    build_schema,
    diff_schemas,
    entity_to_table,
)
from nextorm.schema.builder import _target_matches, _target_table_name
from nextorm.schema.ddl import MariaDBRenderer, PostgresRenderer
from nextorm.schema.diff import SchemaOp

# ---------------------------------------------------------------------------
# Test entities (defined at module scope, ordered by dependency)
# ---------------------------------------------------------------------------


class STag(Entity):
    label: Req[str]


class SAuthor(Entity):
    name: Req[str]
    email: Req[str]


# Test Opt[str] and Opt[LongStr] nullable and non-nullable columns
class SPost(Entity):
    title: Req[str]
    slug: Req[str]
    body: Opt[str]
    body_nullable: Opt[str] = Opt(nullable=True)
    long_bio: Opt[LongStr]
    long_bio_nullable: Opt[LongStr] = Opt(nullable=True)
    author: Single[SAuthor]
    tags: Set[STag]


# ---------------------------------------------------------------------------
# Additional DDL tests for Opt[str] and Opt[LongStr] nullable logic
# ---------------------------------------------------------------------------


def test_schema_opt_str_and_longstr_nullable_flags() -> None:
    table = entity_to_table(SPost)
    col_body = next(c for c in table.columns if c.name == "body")
    col_body_nullable = next(c for c in table.columns if c.name == "body_nullable")
    col_long_bio = next(c for c in table.columns if c.name == "long_bio")
    col_long_bio_nullable = next(c for c in table.columns if c.name == "long_bio_nullable")
    assert col_body.nullable is False
    assert col_body_nullable.nullable is True
    assert col_long_bio.nullable is False
    assert col_long_bio_nullable.nullable is True


class SComment(Entity):
    text: Req[str]
    post: Single[SPost]


# Custom-PK entity
class SArticle(Entity):
    custom_id: PK[int]
    title: Req[str]


# Bidirectional M2M pair (for deduplication test)
class SBlogTag(Entity):
    name: Req[str]


class SBlogPost(Entity):
    title: Req[str]
    tags: Set[SBlogTag]
    # SBlogTag also has Set[SBlogPost] — patched below after both are defined


# SPost <-> STag M2M: add posts back-ref on STag
STag._relations_["posts"] = RelationInfo(
    "posts",
    _RS(kind=_RK.SET, target=SPost),
)

# SBlogPost <-> SBlogTag M2M: add posts back-ref on SBlogTag
SBlogTag._relations_["posts"] = RelationInfo(
    "posts",
    _RS(kind=_RK.SET, target=SBlogPost),
)


# ---------------------------------------------------------------------------
# One-to-one test entities (both sides declare Single)
# ---------------------------------------------------------------------------


# Case 1: required vs nullable — O2OProfile (required) is the owning side
class O2OUser(Entity):
    name: Req[str]


class O2OProfile(Entity):
    bio: Opt[str]
    user: Single[O2OUser]  # required → owning side (FK + UNIQUE)


# Add the nullable back-ref to O2OUser after O2OProfile exists
O2OUser._relations_["profile"] = RelationInfo(
    "profile",
    _RS(kind=_RK.SINGLE, target=O2OProfile, nullable=True),
)


# Case 2: both required — alphabetical: AaaO2O < ZzzO2O → AaaO2O is owner
class ZzzO2O(Entity):
    v: Req[str]


class AaaO2O(Entity):
    v: Req[str]
    zzz_ref: Single[ZzzO2O]  # required; AaaO2O is alphabetically lesser → owner


ZzzO2O._relations_["aaa_ref"] = RelationInfo(
    "aaa_ref",
    _RS(kind=_RK.SINGLE, target=AaaO2O, nullable=False),
)


# Case 3: both required — alphabetical other order: BbbO2O < RrrO2O → BbbO2O is owner
class RrrO2O(Entity):
    v: Req[str]


class BbbO2O(Entity):
    v: Req[str]
    rrr_ref: Single[RrrO2O]  # required; BbbO2O < RrrO2O → BbbO2O is owner


RrrO2O._relations_["bbb_ref"] = RelationInfo(
    "bbb_ref",
    _RS(kind=_RK.SINGLE, target=BbbO2O, nullable=False),
)


# Case 4: explicit owner=True — OwnerExplicit forces itself as owner even though
# both are required and it's alphabetically greater ("ownerexplicit" > "nonowner")
class NonOwner(Entity):
    v: Req[str]


class OwnerExplicit(Entity):
    v: Req[str]
    non_owner_ref: Single[NonOwner] = Single(owner=True)


NonOwner._relations_["owner_ref"] = RelationInfo(
    "owner_ref",
    _RS(kind=_RK.SINGLE, target=OwnerExplicit, nullable=False),
)


# Case 5: explicit owner=False on the back-ref side (alternative spelling of case 4)
class NonOwnerB(Entity):
    v: Req[str]


class OwnerB(Entity):
    v: Req[str]
    non_owner_b_ref: Single[NonOwnerB]


NonOwnerB._relations_["owner_b_ref"] = RelationInfo(
    "owner_b_ref",
    _RS(kind=_RK.SINGLE, target=OwnerB, nullable=False, owner=False),
)


# ---------------------------------------------------------------------------


class TestCoreDataClasses:
    def test_column_defaults(self) -> None:
        col = Column(name="title", py_type=str)
        assert col.nullable is False
        assert col.primary_key is False
        assert col.auto_increment is False
        assert col.unique is False
        assert col.index is False
        assert col.max_len is None
        assert col.sql_default is None

    def test_foreignkey_default_ref_column(self) -> None:
        fk = ForeignKey(name="fk_a__b_id", column="b_id", ref_table="b")
        assert fk.ref_column == "id"
        assert fk.on_delete == "CASCADE"

    def test_index_defaults(self) -> None:
        idx = Index(name="idx_post__slug", columns=["slug"])
        assert idx.unique is False

    def test_table_get_column_found(self) -> None:
        col = Column(name="title", py_type=str)
        table = Table(name="post", columns=[col])
        assert table.get_column("title") is col

    def test_table_get_column_not_found(self) -> None:
        table = Table(name="post")
        assert table.get_column("missing") is None

    def test_table_column_names(self) -> None:
        table = Table(
            name="post",
            columns=[Column("id", int), Column("title", str)],
        )
        assert table.column_names() == ["id", "title"]

    def test_table_entity_cls_none_by_default(self) -> None:
        table = Table(name="join")
        assert table.entity_cls is None


# ---------------------------------------------------------------------------


class TestTargetTableName:
    def test_with_type(self) -> None:
        assert _target_table_name(SAuthor) == "sauthor"

    def test_with_string_forward_ref(self) -> None:
        assert _target_table_name("MyModel") == "mymodel"

    def test_with_typing_forwardref(self) -> None:
        import typing  # noqa: PLC0415

        assert _target_table_name(typing.ForwardRef("MyModel")) == "mymodel"

    def test_with_none_returns_empty(self) -> None:
        assert _target_table_name(None) == ""


# --- Test entities for RelationSpec marker options ---
class X1(Entity):
    pass


class Y1(Entity):
    x: Single[X1] = Single(column="custom_x_id", fk_name="fk_y1__custom_x_id")


class X2(Entity):
    pass


class Y2(Entity):
    x: Single[X2] = Single(columns=["col1", "col2"])


class X3(Entity):
    pass


class Y3(Entity):
    xs: Set[X3] = Set(reverse_column="y_ref_id")


class X4(Entity):
    pass


class Y4(Entity):
    xs: Set[X4] = Set(reverse="ys")


def test_single_relation_column_and_fk_name() -> None:
    table = entity_to_table(Y1)
    col = next(c for c in table.columns if c.name == "custom_x_id")
    fk = next(f for f in table.foreign_keys if f.column == "custom_x_id")
    assert col is not None
    assert fk.name == "fk_y1__custom_x_id"
    assert fk.ref_table == "x1"
    assert fk.ref_column == "id"


def test_single_relation_columns() -> None:
    table = entity_to_table(Y2)
    col1 = next(c for c in table.columns if c.name == "col1")
    col2 = next(c for c in table.columns if c.name == "col2")
    assert col1 is not None
    assert col2 is not None


# --- Test entities for FK column type derivation (regression: previously
# hardcoded py_type=int for every FK column regardless of the target's real
# PK type, e.g. a str-typed sku PK) ---
class XStrPk(Entity):
    sku: PK[str] = PK(20)


class YStrFk(Entity):
    x: Single[XStrPk] = Single()


def test_single_relation_fk_column_matches_target_pk_type() -> None:
    """FK column type/length must match a non-int (e.g. str) target PK.

    Regression test: entity_to_table() used to hardcode py_type=int for every
    generated FK column, which produced a type mismatch (integer vs varchar)
    the moment a Postgres backend tried to add the FK constraint against a
    str-typed primary key such as ``PK[str]``.
    """
    table = entity_to_table(YStrFk)
    fk_col = next(c for c in table.columns if c.name == "x_id")
    fk = next(f for f in table.foreign_keys if f.column == "x_id")
    assert fk_col.py_type is str
    assert fk_col.max_len == 20
    assert fk.ref_table == "xstrpk"
    assert fk.ref_column == "sku"


def test_set_relation_reverse_column_and_reverse_columns() -> None:
    X3._relations_["ys"] = RelationInfo(
        "ys", _RS(kind=_RK.SET, target=Y3, reverse_columns=["x_ref_id"])
    )
    schema = build_schema([X3, Y3])
    join_table = schema["x3_y3"]
    col_names = {c.name for c in join_table.columns}
    assert col_names == {"x3_id", "x_ref_id"}


def test_set_relation_reverse() -> None:
    X4._relations_["ys"] = RelationInfo("ys", _RS(kind=_RK.SET, target=Y4, reverse="xs"))
    schema = build_schema([X4, Y4])
    join_table = schema["x4_y4"]

    assert len(join_table.columns) == 2


class TestTargetMatches:
    def test_identity_match(self) -> None:
        assert _target_matches(SAuthor, SAuthor) is True

    def test_string_match(self) -> None:
        assert _target_matches("sauthor", SAuthor) is True

    def test_forwardref_match(self) -> None:
        import typing  # noqa: PLC0415

        assert _target_matches(typing.ForwardRef("SAuthor"), SAuthor) is True

    def test_unknown_target_returns_false(self) -> None:
        assert _target_matches(42, SAuthor) is False  # type: ignore[arg-type]

    def test_target_matches_none_and_forwardref(self) -> None:
        import typing

        class Dummy:
            __name__ = "Dummy"

        # None target
        assert not _target_matches(None, Dummy)
        # ForwardRef target
        ref = typing.ForwardRef("Dummy")
        assert _target_matches(ref, Dummy)


class TestEntityToTable:
    def test_table_name_is_lowercased_class_name(self) -> None:
        table = entity_to_table(SPost)
        assert table.name == "spost"

    def test_entity_cls_back_reference(self) -> None:
        table = entity_to_table(SPost)
        assert table.entity_cls is SPost

    def test_required_field_not_nullable(self) -> None:
        table = entity_to_table(SPost)
        col = table.get_column("title")
        assert col is not None
        assert col.py_type is str
        assert col.nullable is False

    def test_optional_field_is_nullable(self) -> None:
        table = entity_to_table(SPost)
        col = table.get_column("body")
        assert col is not None
        # Opt[str] is not nullable by default
        assert col.nullable is False

    def test_auto_pk_column_present(self) -> None:
        table = entity_to_table(SPost)
        pk_col = table.get_column("id")
        assert pk_col is not None
        assert pk_col.primary_key is True
        assert pk_col.auto_increment is True

    def test_explicit_pk_no_auto_id(self) -> None:
        table = entity_to_table(SArticle)
        assert table.get_column("custom_id") is not None
        assert table.get_column("id") is None

    def test_custom_column_name_via_spec(self) -> None:
        from nextorm.entity import Entity as _E
        from nextorm.fields import FieldSpec

        class CustomCol(_E):
            full_name: Req[str]

        # Manually patch the spec to have a column alias
        from nextorm.entity import FieldInfo

        CustomCol._fields_["full_name"] = FieldInfo("full_name", str, FieldSpec(column="fullname"))
        table = entity_to_table(CustomCol)
        assert table.get_column("fullname") is not None
        assert table.get_column("full_name") is None

    def test_indexed_field_creates_index_entry(self) -> None:
        from nextorm.entity import Entity as _E
        from nextorm.entity import FieldInfo
        from nextorm.fields import FieldSpec

        class Indexed(_E):
            slug: Req[str]

        # Patch spec to set index=True
        Indexed._fields_["slug"] = FieldInfo("slug", str, FieldSpec(index=True))
        table = entity_to_table(Indexed)
        assert len(table.indexes) == 1
        assert table.indexes[0].name == "idx_indexed__slug"
        assert table.indexes[0].columns == ["slug"]

    def test_unique_field_does_not_create_index_entry(self) -> None:
        from nextorm.entity import Entity as _E
        from nextorm.entity import FieldInfo
        from nextorm.fields import FieldSpec

        class UniqueField(_E):
            code: Req[str]

        UniqueField._fields_["code"] = FieldInfo("code", str, FieldSpec(unique=True))
        table = entity_to_table(UniqueField)
        assert not table.indexes  # UNIQUE is on column def, not a separate index

    def test_onetomany_produces_no_column(self) -> None:
        """Set relations must not add a column to the owner table."""

        class SPost2(Entity):
            title: Req[str]
            comments: Set[SComment]

        table = entity_to_table(SPost2)
        assert "comments_id" not in table.column_names()
        assert "comments" not in table.column_names()


# ---------------------------------------------------------------------------


class TestManyToOneFK:
    def test_fk_column_added(self) -> None:
        table = entity_to_table(SComment)
        assert "post_id" in table.column_names()

    def test_fk_column_type_is_int(self) -> None:
        table = entity_to_table(SComment)
        col = table.get_column("post_id")
        assert col is not None
        assert col.py_type is int
        assert col.nullable is False

    def test_fk_constraint_present(self) -> None:
        table = entity_to_table(SComment)
        assert len(table.foreign_keys) == 1
        fk = table.foreign_keys[0]
        assert fk.column == "post_id"
        assert fk.ref_table == "spost"
        assert fk.ref_column == "id"
        assert fk.on_delete == "CASCADE"
        assert fk.name == "fk_scomment__post_id"

    def test_required_fk_is_not_null_with_cascade(self) -> None:
        """Single[T] → NOT NULL column, ON DELETE CASCADE."""
        table = entity_to_table(SComment)
        col = table.get_column("post_id")
        assert col is not None
        assert col.nullable is False
        fk = table.foreign_keys[0]
        assert fk.on_delete == "CASCADE"

    def test_optional_fk_is_nullable_with_set_null(self) -> None:
        """Single[T | None] → NULLABLE column, ON DELETE SET NULL."""
        from nextorm.entity import Entity as _E

        class WithOptFK(_E):
            owner: Single[SAuthor | None]

        table = entity_to_table(WithOptFK)
        col = table.get_column("owner_id")
        assert col is not None
        assert col.nullable is True
        fk = next(f for f in table.foreign_keys if f.column == "owner_id")
        assert fk.on_delete == "SET NULL"

    def test_cascade_delete_true_overrides_nullable(self) -> None:
        """cascade_delete=True forces CASCADE even when FK is nullable."""
        from nextorm.entity import Entity as _E
        from nextorm.entity import RelationInfo
        from nextorm.fields import RelationKind, RelationSpec

        class WithOverride(_E):
            parent: Single[SAuthor | None]

        WithOverride._relations_["parent"] = RelationInfo(
            "parent",
            RelationSpec(
                kind=RelationKind.SINGLE,
                target=SAuthor,
                nullable=True,
                cascade_delete=True,
            ),
        )
        table = entity_to_table(WithOverride)
        fk = next(f for f in table.foreign_keys if f.column == "parent_id")
        assert fk.on_delete == "CASCADE"

    def test_cascade_delete_false_gives_restrict(self) -> None:
        """cascade_delete=False forces RESTRICT."""
        from nextorm.entity import Entity as _E
        from nextorm.entity import RelationInfo
        from nextorm.fields import RelationKind, RelationSpec

        class WithRestrict(_E):
            parent: Single[SAuthor]

        WithRestrict._relations_["parent"] = RelationInfo(
            "parent",
            RelationSpec(
                kind=RelationKind.SINGLE,
                target=SAuthor,
                nullable=False,
                cascade_delete=False,
            ),
        )
        table = entity_to_table(WithRestrict)
        fk = next(f for f in table.foreign_keys if f.column == "parent_id")
        assert fk.on_delete == "RESTRICT"

    def test_fk_target_as_string(self) -> None:
        """String forward-reference targets must be lowercased for the table name."""
        from nextorm.entity import Entity as _E
        from nextorm.entity import RelationInfo
        from nextorm.fields import RelationKind, RelationSpec

        class WithStringRef(_E):
            parent: Single[SAuthor]

        # Patch to use a string target
        WithStringRef._relations_["parent"] = RelationInfo(
            "parent",
            RelationSpec(kind=RelationKind.SINGLE, target="SAuthor"),
        )
        table = entity_to_table(WithStringRef)
        fk = next(f for f in table.foreign_keys if f.column == "parent_id")
        assert fk.ref_table == "sauthor"


# ---------------------------------------------------------------------------


class TestBuildSchema:
    def test_entity_tables_present(self) -> None:
        tables = build_schema([SPost, SAuthor, STag])
        assert "spost" in tables
        assert "sauthor" in tables
        assert "stag" in tables

    def test_m2m_join_table_created(self) -> None:
        tables = build_schema([SPost, STag])
        # join name = sorted({"spost", "stag"}) = "spost_stag"
        assert "spost_stag" in tables

    def test_m2m_join_table_columns(self) -> None:
        tables = build_schema([SPost, STag])
        join = tables["spost_stag"]
        names = join.column_names()
        assert "spost_id" in names
        assert "stag_id" in names

    def test_m2m_join_table_foreign_keys(self) -> None:
        tables = build_schema([SPost, STag])
        join = tables["spost_stag"]
        fk_cols = {fk.column for fk in join.foreign_keys}
        assert fk_cols == {"spost_id", "stag_id"}

    def test_m2m_bidirectional_join_table_deduplicated(self) -> None:
        """Two entities with ManyToMany to each other produce exactly one join table."""
        tables = build_schema([SBlogPost, SBlogTag])
        join_tables = [n for n in tables if n not in ("sblogpost", "sblogtag")]
        assert len(join_tables) == 1

    def test_no_extra_tables_for_onetomany(self) -> None:
        class SimplePost(Entity):
            title: Req[str]
            comments: Set[SComment]

        tables = build_schema([SimplePost, SComment])
        # Only simplepost and scomment — no join table
        assert set(tables.keys()) == {"simplepost", "scomment"}

    def test_m2m_string_forward_ref_resolved(self) -> None:
        """Set with a string forward-ref target is resolved via the entity list."""
        from nextorm.entity import RelationInfo
        from nextorm.fields import RelationKind, RelationSpec

        class FRefA(Entity):
            name: Req[str]

        class FRefB(Entity):
            label: Req[str]

        FRefA._relations_["bs"] = RelationInfo(
            "bs", RelationSpec(kind=RelationKind.SET, target="FRefB")
        )
        FRefB._relations_["as_"] = RelationInfo(
            "as_", RelationSpec(kind=RelationKind.SET, target=FRefA)
        )
        tables = build_schema([FRefA, FRefB])
        join_tables = [n for n in tables if n not in ("frefa", "frefb")]
        assert len(join_tables) == 1

    def test_m2m_unresolvable_string_ref_skipped(self) -> None:
        """A Set with an unresolvable string target produces no join table."""
        from nextorm.entity import RelationInfo
        from nextorm.fields import RelationKind, RelationSpec

        class Orphan(Entity):
            name: Req[str]

        Orphan._relations_["others"] = RelationInfo(
            "others", RelationSpec(kind=RelationKind.SET, target="NonExistent")
        )
        tables = build_schema([Orphan])
        assert set(tables.keys()) == {"orphan"}  # no join table created


# ---------------------------------------------------------------------------


class TestOneToOneSchema:
    """``Single`` on both sides → one-to-one: FK + UNIQUE on owning side only."""

    def test_required_vs_nullable_owning_side(self) -> None:
        """Required Single is the owning side; nullable Single has no FK column."""
        # O2OProfile is listed first so it processes as 'a' (required) vs 'b' (nullable)
        # → hits the `if not a_ri.nullable and b_ri.nullable:` branch
        tables = build_schema([O2OProfile, O2OUser])
        user_table = tables["o2ouser"]
        profile_table = tables["o2oprofile"]

        # Owning side (O2OProfile): has user_id FK with UNIQUE
        assert "user_id" in profile_table.column_names()
        user_id_col = profile_table.get_column("user_id")
        assert user_id_col is not None
        assert user_id_col.unique is True
        assert user_id_col.nullable is False
        assert len(profile_table.foreign_keys) == 1
        assert profile_table.foreign_keys[0].on_delete == "CASCADE"

        # Non-owning side (O2OUser): no profile_id column
        assert "profile_id" not in user_table.column_names()
        assert len(user_table.foreign_keys) == 0

    def test_nullable_side_has_no_fk_column(self) -> None:
        """The nullable Single produces no FK column (non-owning back-ref)."""
        # O2OUser is listed first so it processes as 'a' (nullable) vs 'b' (required)
        # → hits the `elif a_ri.nullable and not b_ri.nullable:` branch
        tables = build_schema([O2OUser, O2OProfile])
        user_table = tables["o2ouser"]
        # O2OUser.profile is nullable → non-owning → no profile_id column
        assert "profile_id" not in user_table.column_names()

    def test_both_required_alphabetical_lesser_is_owner(self) -> None:
        """Both required: alphabetically lesser table name ('aaao2o') is the owner."""
        # AaaO2O is listed first → a_name="aaao2o", b_name="zzzo2o", a<=b → owner=a branch
        tables = build_schema([AaaO2O, ZzzO2O])
        aaa_table = tables["aaao2o"]
        zzz_table = tables["zzzo2o"]

        # AaaO2O.zzz_ref is the owner (aaao2o < zzzo2o)
        assert "zzz_ref_id" in aaa_table.column_names()
        col = aaa_table.get_column("zzz_ref_id")
        assert col is not None and col.unique is True

        # ZzzO2O.aaa_ref is non-owning → no column
        assert "aaa_ref_id" not in zzz_table.column_names()

    def test_both_required_alphabetical_other_direction(self) -> None:
        """Both required: when 'rrr' > 'bbb' alphabetically, 'bbb' entity still owns."""
        # RrrO2O is listed first → a_name="rrro2o", b_name="bbbo2o", a>b → else branch (b owns)
        tables = build_schema([RrrO2O, BbbO2O])
        bbb_table = tables["bbbo2o"]
        rrr_table = tables["rrro2o"]

        assert "rrr_ref_id" in bbb_table.column_names()
        col = bbb_table.get_column("rrr_ref_id")
        assert col is not None and col.unique is True
        assert "bbb_ref_id" not in rrr_table.column_names()

    def test_entity_to_table_standalone_no_unique(self) -> None:
        """Standalone entity_to_table (no context) creates FK without UNIQUE."""
        # The O2OProfile entity has a Single[O2OUser] relation
        table = entity_to_table(O2OProfile)
        assert "user_id" in table.column_names()
        col = table.get_column("user_id")
        assert col is not None
        assert col.unique is False  # no O2O context when called standalone

    def test_explicit_owner_true_overrides_alphabetical(self) -> None:
        """owner=True on a RelationSpec forces that side to be the FK owner."""
        # OwnerExplicit > NonOwner alphabetically, but owner=True wins
        # OwnerExplicit is listed first → a=ownerexplicit with owner=True → owner=a branch
        tables = build_schema([OwnerExplicit, NonOwner])
        owner_table = tables["ownerexplicit"]
        non_table = tables["nonowner"]

        assert "non_owner_ref_id" in owner_table.column_names()
        col = owner_table.get_column("non_owner_ref_id")
        assert col is not None and col.unique is True
        assert "owner_ref_id" not in non_table.column_names()

    def test_explicit_owner_false_on_back_ref_side(self) -> None:
        """owner=False on NonOwnerB's back-ref forces OwnerB to be the FK owner."""
        # NonOwnerB is listed first → a=nonownerb with owner=False → b is owner branch
        tables = build_schema([NonOwnerB, OwnerB])
        owner_table = tables["ownerb"]
        non_table = tables["nonownerb"]

        assert "non_owner_b_ref_id" in owner_table.column_names()
        col = owner_table.get_column("non_owner_b_ref_id")
        assert col is not None and col.unique is True
        assert "owner_b_ref_id" not in non_table.column_names()

    def test_relation_spec_class_value_sets_owner(self) -> None:
        """Providing RelationSpec as class-level value passes owner= through EntityMeta."""
        # OwnerExplicit.non_owner_ref was annotated with RelationSpec(owner=True)
        ri = OwnerExplicit._relations_["non_owner_ref"]
        assert ri.spec.owner is True


# ---------------------------------------------------------------------------


class TestDiffSchemas:
    def _table(self, name: str, cols: list[str]) -> Table:
        columns = [Column(c, str) for c in cols]
        return Table(name=name, columns=columns)

    def test_create_table(self) -> None:
        t = self._table("post", ["id", "title"])
        ops = diff_schemas({}, {"post": t})
        assert len(ops) == 1
        assert isinstance(ops[0], CreateTable)
        assert ops[0].table is t

    def test_drop_table(self) -> None:
        t = self._table("post", ["id"])
        ops = diff_schemas({"post": t}, {})
        assert len(ops) == 1
        assert isinstance(ops[0], DropTable)
        assert ops[0].table_name == "post"

    def test_add_column(self) -> None:
        old = self._table("post", ["id", "title"])
        new = self._table("post", ["id", "title", "body"])
        ops = diff_schemas({"post": old}, {"post": new})
        adds = [o for o in ops if isinstance(o, AddColumn)]
        assert len(adds) == 1
        assert adds[0].column.name == "body"

    def test_drop_column(self) -> None:
        old = self._table("post", ["id", "title", "body"])
        new = self._table("post", ["id", "title"])
        ops = diff_schemas({"post": old}, {"post": new})
        drops = [o for o in ops if isinstance(o, DropColumn)]
        assert len(drops) == 1
        assert drops[0].column_name == "body"

    def test_add_index(self) -> None:
        idx = Index(name="idx_post__slug", columns=["slug"])
        old = Table(name="post", columns=[Column("id", int)])
        new = Table(name="post", columns=[Column("id", int)], indexes=[idx])
        ops = diff_schemas({"post": old}, {"post": new})
        adds = [o for o in ops if isinstance(o, AddIndex)]
        assert len(adds) == 1
        assert adds[0].index is idx

    def test_drop_index(self) -> None:
        idx = Index(name="idx_post__slug", columns=["slug"])
        old = Table(name="post", columns=[Column("id", int)], indexes=[idx])
        new = Table(name="post", columns=[Column("id", int)])
        ops = diff_schemas({"post": old}, {"post": new})
        drops = [o for o in ops if isinstance(o, DropIndex)]
        assert len(drops) == 1
        assert drops[0].index_name == "idx_post__slug"

    def test_no_ops_when_identical(self) -> None:
        t = self._table("post", ["id", "title"])
        ops = diff_schemas({"post": t}, {"post": t})
        assert ops == []

    def test_unchanged_index_produces_no_ops(self) -> None:
        """An index present in both current and target must not appear in ops."""
        idx = Index(name="idx_post__slug", columns=["slug"])
        t = Table(name="post", columns=[Column("id", int)], indexes=[idx])
        ops = diff_schemas({"post": t}, {"post": t})
        assert not any(isinstance(o, (AddIndex, DropIndex)) for o in ops)


# ---------------------------------------------------------------------------


class TestSQLiteRendererSqlType:
    def setup_method(self) -> None:
        self.r = SQLiteRenderer()

    def test_int(self) -> None:
        assert self.r.sql_type(Column("x", int)) == "INTEGER"

    def test_str(self) -> None:
        assert self.r.sql_type(Column("x", str)) == "TEXT"

    def test_float(self) -> None:
        assert self.r.sql_type(Column("x", float)) == "REAL"

    def test_bool(self) -> None:
        assert self.r.sql_type(Column("x", bool)) == "INTEGER"

    def test_bytes(self) -> None:
        assert self.r.sql_type(Column("x", bytes)) == "BLOB"

    def test_datetime(self) -> None:
        assert self.r.sql_type(Column("x", datetime.datetime)) == "TIMESTAMP"

    def test_date(self) -> None:
        assert self.r.sql_type(Column("x", datetime.date)) == "DATE"

    def test_decimal(self) -> None:
        assert self.r.sql_type(Column("x", decimal.Decimal)) == "NUMERIC"

    def test_varchar_uses_max_len(self) -> None:
        assert self.r.sql_type(Column("x", str, max_len=50)) == "VARCHAR(50)"

    def test_unknown_type_falls_back_to_text(self) -> None:
        assert self.r.sql_type(Column("x", complex)) == "TEXT"  # type: ignore

    def test_sql_type_override_takes_precedence(self) -> None:
        col = Column("data", bytes, sql_type_override="JSONB")
        assert self.r.sql_type(col) == "JSONB"


# ---------------------------------------------------------------------------


class TestSQLiteRendererColumnDef:
    def setup_method(self) -> None:
        self.r = SQLiteRenderer()

    def test_pk_with_autoincrement(self) -> None:
        col = Column("id", int, primary_key=True, auto_increment=True)
        assert self.r._column_def(col) == '"id" INTEGER PRIMARY KEY AUTOINCREMENT'

    def test_pk_without_autoincrement(self) -> None:
        col = Column("id", int, primary_key=True, auto_increment=False)
        assert self.r._column_def(col) == '"id" INTEGER PRIMARY KEY'

    def test_required_not_null(self) -> None:
        col = Column("name", str)
        assert self.r._column_def(col) == '"name" TEXT NOT NULL'

    def test_nullable_no_constraint(self) -> None:
        col = Column("bio", str, nullable=True)
        assert self.r._column_def(col) == '"bio" TEXT'

    def test_unique(self) -> None:
        col = Column("email", str, unique=True)
        assert self.r._column_def(col) == '"email" TEXT NOT NULL UNIQUE'

    def test_sql_default(self) -> None:
        col = Column("status", str, sql_default="'active'")
        assert self.r._column_def(col) == "\"status\" TEXT NOT NULL DEFAULT 'active'"


# ---------------------------------------------------------------------------


class TestSQLiteRendererStatements:
    def setup_method(self) -> None:
        self.r = SQLiteRenderer()

    def test_create_table_basic(self) -> None:
        table = Table(
            name="post",
            columns=[
                Column("id", int, primary_key=True, auto_increment=True),
                Column("title", str),
            ],
        )
        sql = self.r.create_table(table)
        assert 'CREATE TABLE IF NOT EXISTS "post"' in sql
        assert '"id" INTEGER PRIMARY KEY AUTOINCREMENT' in sql
        assert '"title" TEXT NOT NULL' in sql

    def test_create_table_with_fk(self) -> None:
        table = Table(
            name="comment",
            columns=[
                Column("id", int, primary_key=True, auto_increment=True),
                Column("post_id", int),
            ],
            foreign_keys=[
                ForeignKey(
                    name="fk_comment__post_id",
                    column="post_id",
                    ref_table="post",
                    ref_column="id",
                    on_delete="CASCADE",
                )
            ],
        )
        sql = self.r.create_table(table)
        assert 'CONSTRAINT fk_comment__post_id FOREIGN KEY ("post_id")' in sql
        assert 'REFERENCES "post" ("id") ON DELETE CASCADE' in sql

    def test_drop_table(self) -> None:
        assert self.r.drop_table("post") == 'DROP TABLE IF EXISTS "post"'

    def test_add_column(self) -> None:
        col = Column("body", str, nullable=True)
        assert self.r.add_column("post", col) == 'ALTER TABLE "post" ADD COLUMN "body" TEXT'

    def test_drop_column(self) -> None:
        assert self.r.drop_column("post", "body") == 'ALTER TABLE "post" DROP COLUMN "body"'

    def test_create_index_plain(self) -> None:
        idx = Index(name="idx_post__slug", columns=["slug"])
        sql = self.r.create_index("post", idx)
        assert sql == 'CREATE INDEX IF NOT EXISTS idx_post__slug ON "post" ("slug")'

    def test_create_index_unique(self) -> None:
        idx = Index(name="idx_post__slug", columns=["slug"], unique=True)
        sql = self.r.create_index("post", idx)
        assert sql == 'CREATE UNIQUE INDEX IF NOT EXISTS idx_post__slug ON "post" ("slug")'

    def test_drop_index(self) -> None:
        assert self.r.drop_index("idx_post__slug") == "DROP INDEX IF EXISTS idx_post__slug"


# ---------------------------------------------------------------------------


class TestDDLRendererDispatch:
    """Verify that DDLRenderer.render() dispatches every SchemaOp variant."""

    def setup_method(self) -> None:
        self.r = SQLiteRenderer()

    def test_render_create_table(self) -> None:
        op: SchemaOp = CreateTable(Table(name="t", columns=[Column("id", int)]))
        sql = self.r.render(op)
        assert "CREATE TABLE" in sql

    def test_render_drop_table(self) -> None:
        op: SchemaOp = DropTable(table_name="t")
        assert self.r.render(op) == 'DROP TABLE IF EXISTS "t"'

    def test_render_add_column(self) -> None:
        op: SchemaOp = AddColumn(table_name="t", column=Column("x", int))
        sql = self.r.render(op)
        assert "ADD COLUMN" in sql

    def test_render_drop_column(self) -> None:
        op: SchemaOp = DropColumn(table_name="t", column_name="x")
        sql = self.r.render(op)
        assert "DROP COLUMN" in sql

    def test_render_add_index(self) -> None:
        op: SchemaOp = AddIndex(table_name="t", index=Index(name="idx_t__x", columns=["x"]))
        sql = self.r.render(op)
        assert "CREATE INDEX" in sql

    def test_render_drop_index(self) -> None:
        op: SchemaOp = DropIndex(table_name="t", index_name="idx_t__x")
        sql = self.r.render(op)
        assert "DROP INDEX" in sql


# ---------------------------------------------------------------------------
# PostgresRenderer
# ---------------------------------------------------------------------------


class TestPostgresRendererSqlType:
    def setup_method(self) -> None:
        self.r = PostgresRenderer()

    def test_int(self) -> None:
        assert self.r.sql_type(Column("x", int)) == "INTEGER"

    def test_serial_when_pk_and_auto(self) -> None:
        col = Column("id", int, primary_key=True, auto_increment=True)
        assert self.r.sql_type(col) == "SERIAL"

    def test_bigserial_for_non_int_pk(self) -> None:
        # Any pk+auto_increment on a non-int type falls back to BIGSERIAL
        col = Column("id", str, primary_key=True, auto_increment=True)
        assert self.r.sql_type(col) == "BIGSERIAL"

    def test_bool(self) -> None:
        assert self.r.sql_type(Column("flag", bool)) == "BOOLEAN"

    def test_float(self) -> None:
        assert self.r.sql_type(Column("v", float)) == "DOUBLE PRECISION"

    def test_bytes(self) -> None:
        assert self.r.sql_type(Column("data", bytes)) == "BYTEA"

    def test_datetime(self) -> None:
        assert self.r.sql_type(Column("ts", datetime.datetime)) == "TIMESTAMP"

    def test_date(self) -> None:
        assert self.r.sql_type(Column("d", datetime.date)) == "DATE"

    def test_decimal(self) -> None:
        assert self.r.sql_type(Column("amt", decimal.Decimal)) == "NUMERIC"

    def test_varchar(self) -> None:
        assert self.r.sql_type(Column("s", str, max_len=100)) == "VARCHAR(100)"

    def test_unknown_type_falls_back_to_text(self) -> None:
        assert self.r.sql_type(Column("x", list)) == "TEXT"  # type: ignore

    def test_sql_type_override_takes_precedence(self) -> None:
        col = Column("data", bytes, sql_type_override="JSONB")
        assert self.r.sql_type(col) == "JSONB"


class TestPostgresRendererStatements:
    def setup_method(self) -> None:
        self.r = PostgresRenderer()

    def test_create_table_basic(self) -> None:
        table = Table(
            name="post",
            columns=[
                Column("id", int, primary_key=True, auto_increment=True),
                Column("title", str, nullable=False),
            ],
        )
        sql = self.r.create_table(table)
        assert 'CREATE TABLE IF NOT EXISTS "post"' in sql
        assert '"id" SERIAL PRIMARY KEY' in sql
        assert '"title" TEXT NOT NULL' in sql

    def test_create_table_with_fk(self) -> None:
        table = Table(
            name="comment",
            columns=[Column("post_id", int, nullable=False)],
            foreign_keys=[ForeignKey("fk_comment__post_id", "post_id", "post")],
        )
        sql = self.r.create_table(table)
        assert (
            'CONSTRAINT fk_comment__post_id FOREIGN KEY ("post_id") REFERENCES "post" ("id")' in sql
        )

    def test_create_table_nullable_column(self) -> None:
        table = Table(name="t", columns=[Column("notes", str, nullable=True)])
        sql = self.r.create_table(table)
        assert "NOT NULL" not in sql

    def test_create_table_unique_column(self) -> None:
        table = Table(name="t", columns=[Column("email", str, unique=True, nullable=False)])
        sql = self.r.create_table(table)
        assert "UNIQUE" in sql

    def test_create_table_default(self) -> None:
        table = Table(
            name="t",
            columns=[Column("created_at", str, sql_default="CURRENT_TIMESTAMP")],
        )
        sql = self.r.create_table(table)
        assert "DEFAULT CURRENT_TIMESTAMP" in sql

    def test_drop_table(self) -> None:
        assert self.r.drop_table("post") == 'DROP TABLE IF EXISTS "post"'

    def test_add_column(self) -> None:
        col = Column("body", str, nullable=True)
        sql = self.r.add_column("post", col)
        assert sql == 'ALTER TABLE "post" ADD COLUMN IF NOT EXISTS "body" TEXT'

    def test_add_column_not_null(self) -> None:
        col = Column("views", int, nullable=False)
        sql = self.r.add_column("post", col)
        assert "NOT NULL" in sql

    def test_drop_column(self) -> None:
        sql = self.r.drop_column("post", "body")
        assert sql == 'ALTER TABLE "post" DROP COLUMN IF EXISTS "body"'

    def test_create_index(self) -> None:
        idx = Index("idx_post__title", ["title"])
        sql = self.r.create_index("post", idx)
        assert sql == 'CREATE INDEX IF NOT EXISTS idx_post__title ON "post" ("title")'

    def test_create_unique_index(self) -> None:
        idx = Index("unq_post__slug", ["slug"], unique=True)
        sql = self.r.create_index("post", idx)
        assert sql == 'CREATE UNIQUE INDEX IF NOT EXISTS unq_post__slug ON "post" ("slug")'

    def test_drop_index(self) -> None:
        assert self.r.drop_index("idx_post__title") == "DROP INDEX IF EXISTS idx_post__title"

    def test_render_drop_index_op(self) -> None:
        op: SchemaOp = DropIndex(table_name="post", index_name="idx_post__title")
        assert self.r.render(op) == "DROP INDEX IF EXISTS idx_post__title"

    def test_alter_column_type_returns_statement(self) -> None:
        col = Column("amount", decimal.Decimal, precision=8, scale=2, nullable=False)
        sql = self.r.alter_column_type("post", col)
        # Should be a real ALTER statement, not a comment
        assert sql.startswith('ALTER TABLE "post" ALTER COLUMN "amount" TYPE NUMERIC(8, 2)')

    def test_sql_type_uuid_ulid_override(self) -> None:
        import nextorm.fields as _fields

        col = Column("uuid_col", _fields.uuid4)
        assert self.r.sql_type(col) == "UUID"
        col2 = Column("ulid_col", _fields.ulid)
        assert self.r.sql_type(col2) == "CHAR(26)"

    def test_sql_type_override_takes_precedence_ulid(self) -> None:
        col = Column("ulid_col", str, sql_type_override="BINARY(16)")
        assert self.r.sql_type(col) == "BINARY(16)"


# ---------------------------------------------------------------------------
# MariaDBRenderer
# ---------------------------------------------------------------------------


class TestMariaDBRendererSqlType:
    def setup_method(self) -> None:
        self.r = MariaDBRenderer()

    def test_int(self) -> None:
        assert self.r.sql_type(Column("x", int)) == "INT"

    def test_bool(self) -> None:
        assert self.r.sql_type(Column("flag", bool)) == "TINYINT(1)"

    def test_float(self) -> None:
        assert self.r.sql_type(Column("v", float)) == "DOUBLE"

    def test_bytes(self) -> None:
        assert self.r.sql_type(Column("data", bytes)) == "BLOB"

    def test_datetime(self) -> None:
        assert self.r.sql_type(Column("ts", datetime.datetime)) == "DATETIME"

    def test_date(self) -> None:
        assert self.r.sql_type(Column("d", datetime.date)) == "DATE"

    def test_decimal(self) -> None:
        assert self.r.sql_type(Column("amt", decimal.Decimal)) == "DECIMAL"

    def test_varchar(self) -> None:
        assert self.r.sql_type(Column("s", str, max_len=80)) == "VARCHAR(80)"

    def test_unknown_falls_back_to_text(self) -> None:
        assert self.r.sql_type(Column("x", list)) == "TEXT"  # type: ignore

    def test_sql_type_override_takes_precedence(self) -> None:
        col = Column("data", bytes, sql_type_override="JSON")
        assert self.r.sql_type(col) == "JSON"


class TestMariaDBRendererStatements:
    def setup_method(self) -> None:
        self.r = MariaDBRenderer()

    def test_create_table_basic(self) -> None:
        table = Table(
            name="post",
            columns=[
                Column("id", int, primary_key=True, auto_increment=True),
                Column("title", str, nullable=False),
            ],
        )
        sql = self.r.create_table(table)
        assert "CREATE TABLE IF NOT EXISTS `post`" in sql
        assert "`id` INT AUTO_INCREMENT PRIMARY KEY" in sql
        assert "`title` TEXT NOT NULL" in sql

    def test_create_table_pk_without_auto_increment(self) -> None:
        table = Table(
            name="t",
            columns=[Column("id", int, primary_key=True, auto_increment=False)],
        )
        sql = self.r.create_table(table)
        assert "AUTO_INCREMENT" not in sql
        assert "PRIMARY KEY" in sql

    def test_create_table_with_fk(self) -> None:
        table = Table(
            name="comment",
            columns=[Column("post_id", int, nullable=False)],
            foreign_keys=[ForeignKey("fk_comment__post_id", "post_id", "post")],
        )
        sql = self.r.create_table(table)
        assert (
            "CONSTRAINT fk_comment__post_id FOREIGN KEY (`post_id`) REFERENCES `post` (`id`)" in sql
        )

    def test_create_table_unique_column(self) -> None:
        table = Table(name="t", columns=[Column("email", str, unique=True, nullable=False)])
        sql = self.r.create_table(table)
        assert "UNIQUE" in sql

    def test_create_table_default(self) -> None:
        table = Table(
            name="t",
            columns=[Column("created_at", str, sql_default="CURRENT_TIMESTAMP")],
        )
        sql = self.r.create_table(table)
        assert "DEFAULT CURRENT_TIMESTAMP" in sql

    def test_drop_table(self) -> None:
        assert self.r.drop_table("post") == "DROP TABLE IF EXISTS `post`"

    def test_add_column(self) -> None:
        col = Column("body", str, nullable=True)
        assert self.r.add_column("post", col) == "ALTER TABLE `post` ADD COLUMN `body` TEXT"

    def test_drop_column(self) -> None:
        assert self.r.drop_column("post", "body") == "ALTER TABLE `post` DROP COLUMN `body`"

    def test_create_index(self) -> None:
        idx = Index("idx_post__title", ["title"])
        assert self.r.create_index("post", idx) == "CREATE INDEX idx_post__title ON `post` (`title`)"

    def test_create_unique_index(self) -> None:
        idx = Index("unq_post__slug", ["slug"], unique=True)
        sql = self.r.create_index("post", idx)
        assert sql == "CREATE UNIQUE INDEX unq_post__slug ON `post` (`slug`)"

    def test_render_drop_index_includes_table(self) -> None:
        op: SchemaOp = DropIndex(table_name="post", index_name="idx_post__title")
        sql = self.r.render(op)
        assert sql == "DROP INDEX idx_post__title ON post"

    def test_render_delegates_non_drop_index(self) -> None:
        op: SchemaOp = DropTable(table_name="post")
        assert self.r.render(op) == "DROP TABLE IF EXISTS `post`"

    def test_drop_column_with_if_exists(self) -> None:
        sql = self.r.drop_column("post", "body")
        assert sql == "ALTER TABLE `post` DROP COLUMN `body`"

    def test_alter_column_type_returns_statement(self) -> None:
        col = Column("amount", decimal.Decimal, precision=8, scale=2, nullable=False)
        sql = self.r.alter_column_type("post", col)
        assert sql.startswith("ALTER TABLE `post` MODIFY COLUMN `amount` DECIMAL(8, 2) NOT NULL")

    def test_sql_type_uuid_ulid_override(self) -> None:
        import nextorm.fields as _fields

        col = Column("uuid_col", _fields.uuid4)
        assert self.r.sql_type(col) == "CHAR(36)"
        col2 = Column("ulid_col", _fields.ulid)
        assert self.r.sql_type(col2) == "CHAR(26)"

    def test_sql_type_override_takes_precedence_ulid(self) -> None:
        col = Column("ulid_col", str, sql_type_override="BINARY(16)")
        assert self.r.sql_type(col) == "BINARY(16)"


# ---------------------------------------------------------------------------
# composite_key / composite_index
# ---------------------------------------------------------------------------


class Booking(Entity):
    slot: Req[int]
    room: Req[int]
    _ck_slot_room_ = composite_key("slot", "room")


class LogEntry(Entity):
    source: Req[str]
    level: Req[str]
    _idx_source_level_ = composite_index("source", "level")


class MultiConstrained(Entity):
    """Entity with both a composite key and a composite index."""

    a: Req[int]
    b: Req[int]
    c: Req[str]
    _ck_a_b_ = composite_key("a", "b")
    _idx_a_c_ = composite_index("a", "c")


class TestCompositeConstraints:
    def test_composite_key_gives_unique_index(self) -> None:
        table = entity_to_table(Booking)
        idx = next((i for i in table.indexes if i.unique), None)
        assert idx is not None
        assert idx.columns == ["slot", "room"]
        assert idx.name == "unq_booking__slot__room"

    def test_composite_index_gives_non_unique_index(self) -> None:
        table = entity_to_table(LogEntry)
        idx = next((i for i in table.indexes if not i.unique), None)
        assert idx is not None
        assert idx.columns == ["source", "level"]
        assert idx.name == "idx_logentry__source__level"

    def test_multiple_constraints_both_emitted(self) -> None:
        table = entity_to_table(MultiConstrained)
        unique_indexes = [i for i in table.indexes if i.unique]
        non_unique_indexes = [i for i in table.indexes if not i.unique]
        assert len(unique_indexes) == 1
        assert unique_indexes[0].columns == ["a", "b"]
        assert len(non_unique_indexes) == 1
        assert non_unique_indexes[0].columns == ["a", "c"]

    def test_composite_key_ddl_sqlite(self) -> None:
        r = SQLiteRenderer()
        table = entity_to_table(Booking)
        idx = next(i for i in table.indexes if i.unique)
        sql = r.create_index(table.name, idx)
        assert "UNIQUE INDEX" in sql
        assert '"slot", "room"' in sql

    def test_composite_index_ddl_sqlite(self) -> None:
        r = SQLiteRenderer()
        table = entity_to_table(LogEntry)
        idx = next(i for i in table.indexes if not i.unique)
        sql = r.create_index(table.name, idx)
        assert "UNIQUE" not in sql
        assert '"source", "level"' in sql

    def test_composite_key_in_build_schema(self) -> None:
        tables = build_schema([Booking])
        assert "booking" in tables
        table = tables["booking"]
        assert any(i.unique and i.columns == ["slot", "room"] for i in table.indexes)

    def test_composite_key_ddl_postgres(self) -> None:
        r = PostgresRenderer()
        table = entity_to_table(Booking)
        idx = next(i for i in table.indexes if i.unique)
        sql = r.create_index(table.name, idx)
        assert "UNIQUE INDEX" in sql
        assert '"slot", "room"' in sql

    def test_composite_key_ddl_mariadb(self) -> None:
        r = MariaDBRenderer()
        table = entity_to_table(Booking)
        idx = next(i for i in table.indexes if i.unique)
        sql = r.create_index(table.name, idx)
        assert "UNIQUE INDEX" in sql
        assert "`slot`, `room`" in sql

    def test_constraints_stored_on_entity_class(self) -> None:
        assert len(Booking._constraints_) == 1
        assert Booking._constraints_[0].unique is True
        assert Booking._constraints_[0].fields == ("slot", "room")

    def test_entity_without_constraints_has_empty_list(self) -> None:
        class Plain(Entity):
            value: Req[int]

        assert Plain._constraints_ == []


# ---------------------------------------------------------------------------
# New types: LongStr, Json, datetime.time
# ---------------------------------------------------------------------------


class TestLongStrDDL:
    """LongStr maps to LONGTEXT (MariaDB), TEXT (SQLite/PostgreSQL)."""

    sqlite = SQLiteRenderer()
    postgres = PostgresRenderer()
    mariadb = MariaDBRenderer()

    def test_sqlite(self) -> None:
        assert self.sqlite.sql_type(Column("body", LongStr)) == "TEXT"

    def test_postgres(self) -> None:
        assert self.postgres.sql_type(Column("body", LongStr)) == "TEXT"

    def test_mariadb(self) -> None:
        assert self.mariadb.sql_type(Column("body", LongStr)) == "LONGTEXT"


class TestJsonDDL:
    """Json maps to JSONB (PostgreSQL), JSON (MariaDB), TEXT (SQLite)."""

    sqlite = SQLiteRenderer()
    postgres = PostgresRenderer()
    mariadb = MariaDBRenderer()

    def test_sqlite(self) -> None:
        assert self.sqlite.sql_type(Column("data", Json)) == "TEXT"

    def test_postgres(self) -> None:
        assert self.postgres.sql_type(Column("data", Json)) == "JSONB"

    def test_mariadb(self) -> None:
        assert self.mariadb.sql_type(Column("data", Json)) == "JSON"


class TestTimeDDL:
    """datetime.time maps to TIME on all providers."""

    sqlite = SQLiteRenderer()
    postgres = PostgresRenderer()
    mariadb = MariaDBRenderer()

    def test_sqlite(self) -> None:
        assert self.sqlite.sql_type(Column("t", datetime.time)) == "TIME"

    def test_postgres(self) -> None:
        assert self.postgres.sql_type(Column("t", datetime.time)) == "TIME"

    def test_mariadb(self) -> None:
        assert self.mariadb.sql_type(Column("t", datetime.time)) == "TIME"


# ---------------------------------------------------------------------------
# precision / scale / unsigned in FieldSpec and Column
# ---------------------------------------------------------------------------


class TestPrecisionScaleDDL:
    """NUMERIC(p, s) / DECIMAL(p, s) DDL from precision + scale on Column."""

    sqlite = SQLiteRenderer()
    postgres = PostgresRenderer()
    mariadb = MariaDBRenderer()

    def test_sqlite_decimal_no_precision(self) -> None:
        assert self.sqlite.sql_type(Column("amt", decimal.Decimal)) == "NUMERIC"

    def test_sqlite_decimal_with_precision_only(self) -> None:
        col = Column("amt", decimal.Decimal, precision=10)
        assert self.sqlite.sql_type(col) == "NUMERIC(10, 0)"

    def test_sqlite_decimal_with_scale_only(self) -> None:
        col = Column("amt", decimal.Decimal, scale=4)
        assert self.sqlite.sql_type(col) == "NUMERIC(10, 4)"

    def test_sqlite_decimal_with_precision_and_scale(self) -> None:
        col = Column("amt", decimal.Decimal, precision=12, scale=3)
        assert self.sqlite.sql_type(col) == "NUMERIC(12, 3)"

    def test_postgres_decimal_with_precision_and_scale(self) -> None:
        col = Column("price", decimal.Decimal, precision=8, scale=2)
        assert self.postgres.sql_type(col) == "NUMERIC(8, 2)"

    def test_postgres_decimal_no_precision(self) -> None:
        assert self.postgres.sql_type(Column("amt", decimal.Decimal)) == "NUMERIC"

    def test_mariadb_decimal_with_precision_and_scale(self) -> None:
        col = Column("price", decimal.Decimal, precision=10, scale=4)
        assert self.mariadb.sql_type(col) == "DECIMAL(10, 4)"

    def test_mariadb_decimal_no_precision(self) -> None:
        assert self.mariadb.sql_type(Column("amt", decimal.Decimal)) == "DECIMAL"


class TestUnsignedDDL:
    """unsigned=True adds UNSIGNED modifier on MariaDB INT only."""

    mariadb = MariaDBRenderer()
    sqlite = SQLiteRenderer()
    postgres = PostgresRenderer()

    def test_mariadb_int_unsigned(self) -> None:
        col = Column("qty", int, unsigned=True)
        assert self.mariadb.sql_type(col) == "INT UNSIGNED"

    def test_mariadb_non_int_unsigned_ignored(self) -> None:
        # unsigned has no effect on non-int types
        col = Column("amt", decimal.Decimal, unsigned=True)
        assert self.mariadb.sql_type(col) == "DECIMAL"

    def test_sqlite_unsigned_ignored(self) -> None:
        col = Column("qty", int, unsigned=True)
        assert self.sqlite.sql_type(col) == "INTEGER"

    def test_postgres_unsigned_ignored(self) -> None:
        col = Column("qty", int, unsigned=True)
        assert self.postgres.sql_type(col) == "INTEGER"


class TestColumnNewDefaults:
    """Column dataclass has sane defaults for new fields."""

    def test_precision_default_none(self) -> None:
        col = Column("x", int)
        assert col.precision is None

    def test_scale_default_none(self) -> None:
        col = Column("x", int)
        assert col.scale is None

    def test_unsigned_default_false(self) -> None:
        col = Column("x", int)
        assert col.unsigned is False


class TestFieldSpecNewDefaults:
    """FieldSpec has sane defaults for new fields."""

    def test_precision_default_none(self) -> None:
        from nextorm.fields import FieldSpec  # noqa: PLC0415

        assert FieldSpec().precision is None

    def test_scale_default_none(self) -> None:
        from nextorm.fields import FieldSpec  # noqa: PLC0415

        assert FieldSpec().scale is None

    def test_unsigned_default_false(self) -> None:
        from nextorm.fields import FieldSpec  # noqa: PLC0415

        assert FieldSpec().unsigned is False


class TestBuilderPassesPrecisionScaleUnsigned:
    """entity_to_table propagates precision/scale/unsigned from FieldSpec to Column."""

    def test_precision_and_scale_pass_through(self) -> None:
        class PriceEntity(Entity):
            amount: Req[decimal.Decimal] = Req(precision=10, scale=2)

        table = entity_to_table(PriceEntity)
        col = table.get_column("amount")
        assert col is not None
        assert col.precision == 10
        assert col.scale == 2

    def test_unsigned_pass_through(self) -> None:
        class CountEntity(Entity):
            qty: Req[int] = Req(unsigned=True)

        table = entity_to_table(CountEntity)
        col = table.get_column("qty")
        assert col is not None
        assert col.unsigned is True

    def test_precision_scale_unsigned_in_ddl(self) -> None:
        class InvoiceItem(Entity):
            price: Req[decimal.Decimal] = Req(precision=8, scale=2)

        table = entity_to_table(InvoiceItem)
        r = MariaDBRenderer()
        sql = r.create_table(table)
        assert "DECIMAL(8, 2)" in sql

    def test_unsigned_int_in_ddl(self) -> None:
        class Stock(Entity):
            quantity: Req[int] = Req(unsigned=True)

        table = entity_to_table(Stock)
        r = MariaDBRenderer()
        sql = r.create_table(table)
        assert "INT UNSIGNED" in sql


# ---------------------------------------------------------------------------
# int size — Column, FieldSpec, builder, and DDL renderers
# ---------------------------------------------------------------------------


class TestColumnSizeDefault:
    """Column has size=None by default."""

    def test_column_size_default_none(self) -> None:
        col = Column("x", int)
        assert col.size is None


class TestFieldSpecSizeDefault:
    """FieldSpec has size=None by default."""

    def test_fieldspec_size_default_none(self) -> None:
        assert FieldSpec().size is None


class TestIntSizeDDL:
    """int column with size= produces the right DDL type across renderers."""

    def test_sqlite_ignores_size_always_integer(self) -> None:
        assert SQLiteRenderer().sql_type(Column("qty", int, size=8)) == "INTEGER"

    def test_sqlite_size_16_still_integer(self) -> None:
        assert SQLiteRenderer().sql_type(Column("qty", int, size=16)) == "INTEGER"

    def test_sqlite_size_64_still_integer(self) -> None:
        assert SQLiteRenderer().sql_type(Column("qty", int, size=64)) == "INTEGER"

    def test_postgres_size_8_is_smallint(self) -> None:
        assert PostgresRenderer().sql_type(Column("qty", int, size=8)) == "SMALLINT"

    def test_postgres_size_16_is_smallint(self) -> None:
        assert PostgresRenderer().sql_type(Column("qty", int, size=16)) == "SMALLINT"

    def test_postgres_size_32_is_integer(self) -> None:
        assert PostgresRenderer().sql_type(Column("qty", int, size=32)) == "INTEGER"

    def test_postgres_size_64_is_bigint(self) -> None:
        assert PostgresRenderer().sql_type(Column("qty", int, size=64)) == "BIGINT"

    def test_mariadb_size_8_is_tinyint(self) -> None:
        assert MariaDBRenderer().sql_type(Column("qty", int, size=8)) == "TINYINT"

    def test_mariadb_size_16_is_smallint(self) -> None:
        assert MariaDBRenderer().sql_type(Column("qty", int, size=16)) == "SMALLINT"

    def test_mariadb_size_32_is_int(self) -> None:
        assert MariaDBRenderer().sql_type(Column("qty", int, size=32)) == "INT"

    def test_mariadb_size_64_is_bigint(self) -> None:
        assert MariaDBRenderer().sql_type(Column("qty", int, size=64)) == "BIGINT"

    def test_mariadb_size_8_unsigned(self) -> None:
        col = Column("qty", int, size=8, unsigned=True)
        assert MariaDBRenderer().sql_type(col) == "TINYINT UNSIGNED"

    def test_int_without_size_unaffected_sqlite(self) -> None:
        assert SQLiteRenderer().sql_type(Column("qty", int)) == "INTEGER"

    def test_int_without_size_unaffected_postgres(self) -> None:
        assert PostgresRenderer().sql_type(Column("qty", int)) == "INTEGER"

    def test_int_without_size_unaffected_mariadb(self) -> None:
        assert MariaDBRenderer().sql_type(Column("qty", int)) == "INT"


class TestBuilderPassesSize:
    """entity_to_table propagates size from FieldSpec to Column."""

    def test_size_pass_through(self) -> None:
        class SmallIntEntity(Entity):
            count: Req[int] = Req(size=16)

        table = entity_to_table(SmallIntEntity)
        col = table.get_column("count")
        assert col is not None
        assert col.size == 16

    def test_size_in_mariadb_ddl(self) -> None:
        class BigEntity(Entity):
            big_id: Req[int] = Req(size=64)

        sql = MariaDBRenderer().create_table(entity_to_table(BigEntity))
        assert "BIGINT" in sql

    def test_size_in_postgres_ddl(self) -> None:
        class ByteEntity(Entity):
            flags: Req[int] = Req(size=8)

        sql = PostgresRenderer().create_table(entity_to_table(ByteEntity))
        assert "SMALLINT" in sql


# ---------------------------------------------------------------------------
# ANN index — Index dataclass and DDL renderers
# ---------------------------------------------------------------------------


class TestANNIndex:
    """Index with method/opclass/with_options produces ANN DDL."""

    def test_index_defaults_none(self) -> None:
        idx = Index("idx", ["col"])
        assert idx.method is None
        assert idx.opclass is None
        assert idx.with_options is None

    def test_postgres_hnsw_basic(self) -> None:
        idx = Index("idx_emb_cos", ["embedding"], method="hnsw", opclass="vector_cosine_ops")
        sql = PostgresRenderer().create_index("article", idx)
        assert "USING hnsw" in sql
        assert "vector_cosine_ops" in sql
        assert "IF NOT EXISTS idx_emb_cos" in sql

    def test_postgres_hnsw_with_options(self) -> None:
        idx = Index(
            "idx_emb_cos",
            ["embedding"],
            method="hnsw",
            opclass="vector_cosine_ops",
            with_options={"m": 16, "ef_construction": 64},
        )
        sql = PostgresRenderer().create_index("article", idx)
        assert "WITH (m=16, ef_construction=64)" in sql

    def test_postgres_ivfflat_no_opclass(self) -> None:
        idx = Index("idx_emb_ivf", ["embedding"], method="ivfflat")
        sql = PostgresRenderer().create_index("article", idx)
        assert 'USING ivfflat ("embedding")' in sql
        assert "WITH" not in sql

    def test_postgres_btree_explicit_stays_standard(self) -> None:
        idx = Index("idx_title", ["title"], method="btree")
        sql = PostgresRenderer().create_index("post", idx)
        assert "USING" not in sql
        assert sql == 'CREATE INDEX IF NOT EXISTS idx_title ON "post" ("title")'

    def test_postgres_no_method_stays_standard(self) -> None:
        idx = Index("idx_title", ["title"])
        sql = PostgresRenderer().create_index("post", idx)
        assert "USING" not in sql

    def test_sqlite_create_index_unchanged(self) -> None:
        idx = Index("idx_name", ["title"])
        sql = SQLiteRenderer().create_index("post", idx)
        assert "USING" not in sql
        assert "IF NOT EXISTS idx_name" in sql

    def test_mariadb_create_index_unchanged(self) -> None:
        idx = Index("idx_name", ["title"])
        sql = MariaDBRenderer().create_index("post", idx)
        assert "idx_name" in sql


# ---------------------------------------------------------------------------
# AlterColumnType — diff detection and DDL rendering
# ---------------------------------------------------------------------------


class TestAlterColumnTypeDiff:
    """diff_schemas emits AlterColumnType when column type attributes change."""

    def test_py_type_change_emits_alter(self) -> None:
        old = Table("t", columns=[Column("age", int)])
        new = Table("t", columns=[Column("age", str)])
        ops = diff_schemas({"t": old}, {"t": new})
        alters = [o for o in ops if isinstance(o, AlterColumnType)]
        assert len(alters) == 1
        assert alters[0].table_name == "t"
        assert alters[0].column.py_type is str
        assert alters[0].column.name == "age"

    def test_max_len_change_emits_alter(self) -> None:
        old = Table("t", columns=[Column("email", str, max_len=100)])
        new = Table("t", columns=[Column("email", str, max_len=200)])
        ops = diff_schemas({"t": old}, {"t": new})
        assert any(isinstance(o, AlterColumnType) and o.column.max_len == 200 for o in ops)

    def test_nullable_change_emits_alter(self) -> None:
        old = Table("t", columns=[Column("notes", str, nullable=True)])
        new = Table("t", columns=[Column("notes", str, nullable=False)])
        ops = diff_schemas({"t": old}, {"t": new})
        assert any(isinstance(o, AlterColumnType) for o in ops)

    def test_precision_change_emits_alter(self) -> None:
        import decimal

        old = Table("t", columns=[Column("amt", decimal.Decimal, precision=8)])
        new = Table("t", columns=[Column("amt", decimal.Decimal, precision=12)])
        ops = diff_schemas({"t": old}, {"t": new})
        assert any(isinstance(o, AlterColumnType) for o in ops)

    def test_scale_change_emits_alter(self) -> None:
        import decimal

        old = Table("t", columns=[Column("amt", decimal.Decimal, scale=2)])
        new = Table("t", columns=[Column("amt", decimal.Decimal, scale=4)])
        ops = diff_schemas({"t": old}, {"t": new})
        assert any(isinstance(o, AlterColumnType) for o in ops)

    def test_sql_type_override_change_emits_alter(self) -> None:
        old = Table("t", columns=[Column("data", bytes, sql_type_override="BLOB")])
        new = Table("t", columns=[Column("data", bytes, sql_type_override="JSONB")])
        ops = diff_schemas({"t": old}, {"t": new})
        assert any(isinstance(o, AlterColumnType) for o in ops)

    def test_unsigned_change_emits_alter(self) -> None:
        old = Table("t", columns=[Column("qty", int, unsigned=False)])
        new = Table("t", columns=[Column("qty", int, unsigned=True)])
        ops = diff_schemas({"t": old}, {"t": new})
        assert any(isinstance(o, AlterColumnType) for o in ops)

    def test_size_change_emits_alter(self) -> None:
        old = Table("t", columns=[Column("val", int, size=16)])
        new = Table("t", columns=[Column("val", int, size=32)])
        ops = diff_schemas({"t": old}, {"t": new})
        assert any(isinstance(o, AlterColumnType) for o in ops)

    def test_identical_column_produces_no_alter(self) -> None:
        col = Column("age", int, nullable=False)
        t = Table("t", columns=[col])
        ops = diff_schemas({"t": t}, {"t": t})
        assert not any(isinstance(o, AlterColumnType) for o in ops)

    def test_alter_carries_target_column(self) -> None:
        """The AlterColumnType.column must be the *target* column descriptor."""
        old = Table("t", columns=[Column("score", int)])
        new_col = Column("score", float, nullable=True)
        new = Table("t", columns=[new_col])
        ops = diff_schemas({"t": old}, {"t": new})
        alters = [o for o in ops if isinstance(o, AlterColumnType)]
        assert len(alters) == 1
        assert alters[0].column is new_col


class TestAlterColumnTypeDDL:
    """Per-renderer DDL for AlterColumnType."""

    def test_sqlite_returns_comment(self) -> None:
        col = Column("age", str)
        stmt = SQLiteRenderer().alter_column_type("user", col)
        assert stmt.startswith("-- SQLite:")
        assert "cannot ALTER COLUMN TYPE" in stmt
        assert "user.age" in stmt

    def test_sqlite_includes_new_type_in_comment(self) -> None:
        col = Column("score", float)
        stmt = SQLiteRenderer().alter_column_type("t", col)
        assert "REAL" in stmt

    def test_postgres_alter_column_type_nullable(self) -> None:
        col = Column("age", str, nullable=True)
        stmt = PostgresRenderer().alter_column_type("user", col)
        assert stmt == 'ALTER TABLE "user" ALTER COLUMN "age" TYPE TEXT'

    def test_postgres_alter_column_type_not_null(self) -> None:
        col = Column("age", int, nullable=False)
        stmt = PostgresRenderer().alter_column_type("post", col)
        assert stmt == 'ALTER TABLE "post" ALTER COLUMN "age" TYPE INTEGER NOT NULL'

    def test_postgres_alter_column_type_varchar(self) -> None:
        col = Column("email", str, max_len=255, nullable=False)
        stmt = PostgresRenderer().alter_column_type("account", col)
        assert 'ALTER COLUMN "email" TYPE VARCHAR(255) NOT NULL' in stmt

    def test_mariadb_modify_column_nullable(self) -> None:
        col = Column("age", str, nullable=True)
        stmt = MariaDBRenderer().alter_column_type("user", col)
        assert stmt == "ALTER TABLE `user` MODIFY COLUMN `age` TEXT"

    def test_mariadb_modify_column_not_null(self) -> None:
        col = Column("age", int, nullable=False)
        stmt = MariaDBRenderer().alter_column_type("post", col)
        assert stmt == "ALTER TABLE `post` MODIFY COLUMN `age` INT NOT NULL"

    def test_mariadb_modify_column_varchar(self) -> None:
        col = Column("name", str, max_len=80, nullable=False)
        stmt = MariaDBRenderer().alter_column_type("profile", col)
        assert "MODIFY COLUMN `name` VARCHAR(80) NOT NULL" in stmt


class TestAlterColumnTypeRenderDispatch:
    """DDLRenderer.render() dispatches AlterColumnType correctly for each renderer."""

    def test_sqlite_render_dispatch(self) -> None:
        op: SchemaOp = AlterColumnType(table_name="t", column=Column("x", str))
        result = SQLiteRenderer().render(op)
        assert "-- SQLite:" in result

    def test_postgres_render_dispatch(self) -> None:
        op: SchemaOp = AlterColumnType(table_name="t", column=Column("x", int, nullable=False))
        result = PostgresRenderer().render(op)
        assert result == 'ALTER TABLE "t" ALTER COLUMN "x" TYPE INTEGER NOT NULL'

    def test_mariadb_render_dispatch(self) -> None:
        op: SchemaOp = AlterColumnType(table_name="t", column=Column("x", int, nullable=False))
        result = MariaDBRenderer().render(op)
        assert result == "ALTER TABLE `t` MODIFY COLUMN `x` INT NOT NULL"


# ---------------------------------------------------------------------------
# DDLRenderer/SQLiteRenderer edge cases
# ---------------------------------------------------------------------------


class DummyRenderer(DDLRenderer):
    def sql_type(self, column: Column) -> str:
        return "DUMMY"

    def create_table(self, table: Table) -> str:
        return "CREATE TABLE dummy"

    def drop_table(self, table_name: str) -> str:
        return "DROP TABLE dummy"

    def add_column(self, table_name: str, column: Column) -> str:
        return "ALTER TABLE dummy ADD COLUMN"

    def drop_column(self, table_name: str, column_name: str) -> str:
        return "ALTER TABLE dummy DROP COLUMN"

    def alter_column_type(self, table_name: str, column: Column) -> str:
        return "ALTER COLUMN TYPE"

    def create_index(self, table_name: str, index: Index) -> str:
        return "CREATE INDEX dummy"

    def drop_index(self, index_name: str) -> str:
        return "DROP INDEX dummy"


def test_ddlrenderer_render_dispatch() -> None:
    r = DummyRenderer()
    t = Table(name="t")
    c = Column(name="c", py_type=int)
    i = Index(name="idx", columns=["c"])
    # Each op type

    assert r.render(CreateTable(table=t)) == "CREATE TABLE dummy"
    assert r.render(DropTable(table_name="t")) == "DROP TABLE dummy"
    assert r.render(AddColumn(table_name="t", column=c)) == "ALTER TABLE dummy ADD COLUMN"
    assert r.render(DropColumn(table_name="t", column_name="c")) == "ALTER TABLE dummy DROP COLUMN"
    assert r.render(AlterColumnType(table_name="t", column=c)) == "ALTER COLUMN TYPE"
    assert r.render(AddIndex(table_name="t", index=i)) == "CREATE INDEX dummy"
    assert r.render(DropIndex(table_name="t", index_name="idx")) == "DROP INDEX dummy"

    # Unknown op triggers assert_never (should raise AssertionError)
    class UnknownOp:
        pass

    with pytest.raises(AssertionError):
        r.render(UnknownOp())  # type: ignore[arg-type]


def test_sqlite_renderer_type_mapping_and_alter_column_type() -> None:
    r = SQLiteRenderer()
    # decimal with precision/scale
    c = Column(name="c", py_type=decimal.Decimal, precision=5, scale=2)
    assert r.sql_type(c) == "NUMERIC(5, 2)"
    # enum
    import enum

    class E(enum.Enum):
        A = 1

    c = Column(name="c", py_type=E)
    assert r.sql_type(c) == "TEXT"
    # uuid
    c = Column(name="c", py_type=uuid.UUID)
    assert r.sql_type(c) == "TEXT"
    # fallback
    c = Column(name="c", py_type=bytes)
    assert r.sql_type(c) == "BLOB"
    # alter_column_type returns comment
    c = Column(name="c", py_type=int)
    out = r.alter_column_type("tbl", c)
    assert out.startswith("-- SQLite: cannot ALTER COLUMN TYPE")


# ---------------------------------------------------------------------------
# v0.2 coverage gaps — builder and DDL
# ---------------------------------------------------------------------------


def test_sqlite_renderer_ulid_type() -> None:
    """SQLiteRenderer must recognise the ulid sentinel type and render TEXT."""
    from nextorm.fields import ulid  # noqa: PLC0415

    r = SQLiteRenderer()
    c = Column(name="code", py_type=ulid)
    assert r.sql_type(c) == "TEXT"


def test_resolve_target_cls_forward_ref() -> None:
    """_resolve_target_cls must handle a typing.ForwardRef as a target."""
    import typing  # noqa: PLC0415

    from nextorm.schema.builder import _resolve_target_cls  # noqa: PLC0415

    class RefEntity(Entity):
        label: Req[str]

    ref = typing.ForwardRef("RefEntity")
    result = _resolve_target_cls(ref, [RefEntity])
    assert result is RefEntity


def test_resolve_target_cls_forward_ref_not_found() -> None:
    """_resolve_target_cls returns None when the ForwardRef name has no match."""
    import typing  # noqa: PLC0415

    from nextorm.schema.builder import _resolve_target_cls  # noqa: PLC0415

    result = _resolve_target_cls(typing.ForwardRef("NoSuchEntity"), [])
    assert result is None


# --- M2M join table column overrides via reverse_column / reverse_columns ---


class _M2MLeft(Entity):
    rights: Set["_M2MRight"]  # noqa: UP037


class _M2MRight(Entity):
    lefts: Set[_M2MLeft]


class _M2MLeftRevCol(Entity):
    rights: Set["_M2MRightRevCol"] = Set(reverse_column="left_custom_id")  # noqa: UP037


class _M2MRightRevCol(Entity):
    lefts: Set[_M2MLeftRevCol]


class _M2MLeftRevCols(Entity):
    rights: Set["_M2MRightRevCols"] = Set(reverse_columns=["left_list_id"])  # noqa: UP037


class _M2MRightRevCols(Entity):
    lefts: Set[_M2MLeftRevCols]


class _M2MLeftCol(Entity):
    rights: Set["_M2MRightCol"] = Set(column="left_explicit_id")  # noqa: UP037


class _M2MRightCol(Entity):
    lefts: Set[_M2MLeftCol]


class _M2MLeftCols(Entity):
    rights: Set["_M2MRightCols"] = Set(columns=["left_multi_id"])  # noqa: UP037


class _M2MRightCols(Entity):
    lefts: Set[_M2MLeftCols]


def test_m2m_reverse_column_overrides_join_column() -> None:
    """reverse_column on a Set should rename the col pointing back to that table."""
    tables = build_schema([_M2MLeftRevCol, _M2MRightRevCol])
    # The join table must contain a column named "left_custom_id"
    join_tables = {
        name: t
        for name, t in tables.items()
        if name not in ("_m2mleftrevcol", "_m2mrigtrevcol", "_m2mrightrevcol")
    }
    join_t = next(iter(join_tables.values()))
    col_names = {c.name for c in join_t.columns}
    assert "left_custom_id" in col_names


def test_m2m_reverse_columns_list_overrides_join_column() -> None:
    """reverse_columns=[...] on a Set should rename the col using the first item."""
    tables = build_schema([_M2MLeftRevCols, _M2MRightRevCols])
    join_tables = {
        name: t
        for name, t in tables.items()
        if name
        not in (
            "_m2mleftrevcolS",
            "_m2mrightrevcolS",
            "_m2mleftrevcols",
            "_m2mrightrevcols",
        )
    }
    join_t = next(iter(join_tables.values()))
    col_names = {c.name for c in join_t.columns}
    assert "left_list_id" in col_names


def test_m2m_column_overrides_join_column_a() -> None:
    """column on a Set should rename the col pointing from the join table to that entity."""
    tables = build_schema([_M2MLeftCol, _M2MRightCol])
    join_tables = {
        name: t for name, t in tables.items() if name not in ("_m2mleftcol", "_m2mrightcol")
    }
    join_t = next(iter(join_tables.values()))
    col_names = {c.name for c in join_t.columns}
    assert "left_explicit_id" in col_names


def test_m2m_columns_list_overrides_join_column_a() -> None:
    """columns=[...] on a Set should rename the col using the first item."""
    tables = build_schema([_M2MLeftCols, _M2MRightCols])
    join_tables = {
        name: t for name, t in tables.items() if name not in ("_m2mleftcols", "_m2mrightcols")
    }
    join_t = next(iter(join_tables.values()))
    col_names = {c.name for c in join_t.columns}
    assert "left_multi_id" in col_names


# ---------------------------------------------------------------------------
# O2O with explicit reverse parameter (build_schema lines 257-317)
# ---------------------------------------------------------------------------


class _RevPassport(Entity):
    """Owning side of an O2O with explicit reverse."""

    number: Req[str]
    holder: Single[_RevHolder] = Single(reverse="passport", nullable=False)


class _RevHolder(Entity):
    """Non-owning side of an O2O with explicit reverse."""

    name: Req[str]
    passport: Single[_RevPassport] = Single(reverse="holder", nullable=True)


class _RevEmployeeA(Entity):
    """Employee side of a mutual Single-Single O2O with owner flag."""

    code: Req[str]
    profile: Single[_RevProfileA] = Single(reverse="employee", nullable=True, owner=False)


class _RevProfileA(Entity):
    """Profile side of a mutual Single-Single O2O."""

    bio: Req[str]
    employee: Single[_RevEmployeeA] = Single(reverse="profile", nullable=False, owner=True)


def test_o2o_with_explicit_reverse_owning_side_gets_fk() -> None:
    """O2O pair with reverse= parameter: non-nullable side is owning (has FK column)."""
    tables = build_schema([_RevPassport, _RevHolder])
    # _RevPassport is the non-nullable Single → should be the owning side
    passport_table = tables["_revpassport"]
    holder_col_names = [c.name for c in passport_table.columns]
    assert "holder_id" in holder_col_names

    # _RevHolder has nullable Single → non-owning side (no FK column for passport)
    holder_table = tables["_revholder"]
    holder_col_names2 = [c.name for c in holder_table.columns]
    assert "passport_id" not in holder_col_names2


def test_o2o_with_explicit_reverse_owner_flag() -> None:
    """O2O pair with owner=True/False flags controls which side has the FK column."""
    tables = build_schema([_RevEmployeeA, _RevProfileA])
    # _RevProfileA has owner=True → its side gets the FK column
    profile_table = tables["_revprofilea"]
    profile_col_names = [c.name for c in profile_table.columns]
    assert "employee_id" in profile_col_names

    # _RevEmployeeA has owner=False → does NOT get a FK column
    emp_table = tables["_revemployeea"]
    emp_col_names = [c.name for c in emp_table.columns]
    assert "profile_id" not in emp_col_names


class _RevSingleSetOwner(Entity):
    """Many-to-one side with explicit reverse pointing to the Set."""

    tag: Req[str]
    group: Single[_RevSetGroup] = Single(reverse="members")


class _RevSetGroup(Entity):
    """One-to-many side with a Set collection."""

    name: Req[str]
    members: Set[_RevSingleSetOwner]


def test_single_set_pair_with_explicit_reverse() -> None:
    """Single-Set pair with explicit reverse= on the Single side is M2O (not O2O)."""
    tables = build_schema([_RevSingleSetOwner, _RevSetGroup])
    # Single side (_RevSingleSetOwner) is owning → has FK column
    owner_table = tables["_revsinglesetowner"]
    col_names = [c.name for c in owner_table.columns]
    # Single side has the FK to the group
    assert "group_id" in col_names


# ---------------------------------------------------------------------------
# schema/builder.py lines 297-298: explicit reverse= O2O where a_ri.spec.owner is True
# When the FIRST processed entity has owner=True, lines 297-298 are hit.
# ---------------------------------------------------------------------------


def test_o2o_with_explicit_reverse_a_is_owner_true() -> None:
    """O2O with reverse=: processing the owner=True entity first hits lines 297-298.

    _RevProfileA (owner=True) is listed first, so it becomes 'a' in the builder.
    Condition: a_ri.spec.owner is True → lines 297-298.
    """
    tables = build_schema([_RevProfileA, _RevEmployeeA])
    # _RevProfileA has owner=True → its side gets the FK column
    profile_table = tables["_revprofilea"]
    profile_col_names = [c.name for c in profile_table.columns]
    assert "employee_id" in profile_col_names

    emp_table = tables["_revemployeea"]
    emp_col_names = [c.name for c in emp_table.columns]
    assert "profile_id" not in emp_col_names


# ---------------------------------------------------------------------------
# schema/builder.py lines 305-307: explicit reverse= O2O where a_ri.spec.nullable
# and not b_ri.spec.nullable
# When the FIRST processed entity has nullable=True and second has nullable=False, lines 305-307 hit.
# ---------------------------------------------------------------------------


def test_o2o_with_explicit_reverse_a_nullable_b_not() -> None:
    """O2O with reverse=: nullable a, non-nullable b → b is owner (lines 305-307).

    Process _RevHolder first (nullable passport), _RevPassport second (non-nullable holder).
    Condition: a_ri.spec.nullable and not b_ri.spec.nullable → owner=b (_RevPassport).
    """
    tables = build_schema([_RevHolder, _RevPassport])
    # _RevPassport is non-nullable → it becomes the owner (has FK column)
    passport_table = tables["_revpassport"]
    passport_col_names = [c.name for c in passport_table.columns]
    assert "holder_id" in passport_col_names

    holder_table = tables["_revholder"]
    holder_col_names = [c.name for c in holder_table.columns]
    assert "passport_id" not in holder_col_names


# ---------------------------------------------------------------------------
# schema/builder.py lines 308-313: explicit reverse= O2O — alphabetical fallback
# When both sides are nullable and no owner= is set, alphabetical order decides.
# ---------------------------------------------------------------------------


class _AAlphaRevA(Entity):
    """First alphabetically — mutual reverse O2O, both nullable."""

    _table_ = "_a_alpha_rev_a"
    note: Req[str]
    partner: Single["_ZAlphaRevB"] = Single(reverse="peer", nullable=True)  # noqa: UP037


class _ZAlphaRevB(Entity):
    """Second alphabetically — mutual reverse O2O, both nullable."""

    _table_ = "_z_alpha_rev_b"
    note: Req[str]
    peer: Single["_AAlphaRevA"] = Single(reverse="partner", nullable=True)  # noqa: UP037


def test_o2o_explicit_reverse_alphabetical_a_first_is_owner() -> None:
    """O2O with reverse= and same nullability: alphabetically-first table owns FK (lines 308-310).

    Both entities nullable, no owner= set. 'a_alpha_rev_a' < 'z_alpha_rev_b' alphabetically
    → a is owner → lines 308-310 (elif a_name <= b_name).
    """
    tables = build_schema([_AAlphaRevA, _ZAlphaRevB])
    a_table = tables["_a_alpha_rev_a"]
    a_col_names = [c.name for c in a_table.columns]
    # _a_alpha_rev_a is alphabetically first → owns FK
    assert "partner_id" in a_col_names

    z_table = tables["_z_alpha_rev_b"]
    z_col_names = [c.name for c in z_table.columns]
    assert "peer_id" not in z_col_names


def test_o2o_explicit_reverse_alphabetical_z_first_processed() -> None:
    """O2O with reverse= and same nullability: Z processed first → else branch (lines 311-313).

    Both entities nullable, no owner= set. When _ZAlphaRevB is listed first (a=Z, b=A),
    a_name='_z_alpha_rev_b' > b_name='_a_alpha_rev_a' → else → lines 311-313: b is owner.
    """
    tables = build_schema([_ZAlphaRevB, _AAlphaRevA])
    a_table = tables["_a_alpha_rev_a"]
    a_col_names = [c.name for c in a_table.columns]
    # _a_alpha_rev_a is alphabetically first → owns FK (b wins in else branch)
    assert "partner_id" in a_col_names


# ---------------------------------------------------------------------------
# schema/builder.py line 258->320: reverse= set but target has no matching relation name
# ---------------------------------------------------------------------------


class _BadReverseOwner(Entity):
    """Entity with reverse= pointing to nonexistent relation name on target."""

    _table_ = "_bad_reverse_owner"
    name: Req[str]
    ref: Single["_BadReverseTarget"] = Single(reverse="nonexistent", nullable=True)  # noqa: UP037


class _BadReverseTarget(Entity):
    """Target entity with no 'nonexistent' relation."""

    _table_ = "_bad_reverse_target"
    label: Req[str]


def test_o2o_explicit_reverse_not_found_falls_through_to_autodetect() -> None:
    """reverse= pointing to nonexistent relation → back_ri=None → falls to auto-detect.

    _BadReverseOwner.ref has reverse='nonexistent' but _BadReverseTarget has no such relation.
    The if back_ri: condition is False → branches to the fallback auto-detection (line 258->320).
    """
    tables = build_schema([_BadReverseOwner, _BadReverseTarget])
    # _BadReverseTarget has no Single back-relation → _BadReverseOwner is plain M2O owner
    owner_table = tables["_bad_reverse_owner"]
    col_names = [c.name for c in owner_table.columns]
    assert "ref_id" in col_names


# ---------------------------------------------------------------------------
# schema/builder.py line 277->320: back_ri found via reverse= but it's SINGLE pointing
# to a DIFFERENT entity (not entity_cls) → falls through to auto-detect
# ---------------------------------------------------------------------------


class _CrossRevC(Entity):
    """Third entity — cross-reference target (not the auto-detect owner)."""

    _table_ = "_cross_rev_c"
    name: Req[str]


class _CrossRevB(Entity):
    """Has a SINGLE pointing to C (not A), but A's reverse= points here."""

    _table_ = "_cross_rev_b"
    cross_ref: Single[_CrossRevC]  # points to C, NOT back to A


class _CrossRevA(Entity):
    """Points to B with reverse='cross_ref', but B.cross_ref points to C, not A."""

    _table_ = "_cross_rev_a"
    name: Req[str]
    ref: Single[_CrossRevB] = Single(reverse="cross_ref", nullable=True)  # noqa: F821


def test_o2o_explicit_reverse_cross_reference_falls_through() -> None:
    """reverse= finds back_ri but it's SINGLE pointing to different entity → line 277->320.

    _CrossRevA.ref has reverse='cross_ref'. _CrossRevB.cross_ref is found but points to C, not A.
    Condition: back_ri.spec.kind == SINGLE and _target_matches(back_ri.target, entity_cls) → False
    → falls through to auto-detect (line 277->320). Since B has no Single back to A, A is plain M2O.
    """
    tables = build_schema([_CrossRevA, _CrossRevB, _CrossRevC])
    owner_table = tables["_cross_rev_a"]
    col_names = [c.name for c in owner_table.columns]
    assert "ref_id" in col_names
