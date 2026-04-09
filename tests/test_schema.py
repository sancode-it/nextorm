"""Tests for schema data structures, builder, diff, and DDL renderers."""

from __future__ import annotations

import datetime
import decimal

from nextorm import PK, Entity, FieldSpec, Opt, Req, Set, Single, composite_index, composite_key
from nextorm.fields import Json, LongStr
from nextorm.schema import (
    AddColumn,
    AddIndex,
    AlterColumnType,
    Column,
    CreateTable,
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


class SPost(Entity):
    title: Req[str]
    slug: Req[str]
    body: Opt[str]
    author: Single[SAuthor]
    tags: Set[STag]


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


# Patch bidirectional back-references so build_schema sees both sides
from nextorm.entity import RelationInfo  # noqa: E402
from nextorm.fields import RelationKind as _RK  # noqa: E402
from nextorm.fields import RelationSpec as _RS  # noqa: E402

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
    non_owner_ref: Single[NonOwner] = _RS(owner=True)  # type: ignore[assignment]


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
        assert col.nullable is True

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
                kind=RelationKind.SINGLE, target=SAuthor, nullable=True, cascade_delete=True
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
                kind=RelationKind.SINGLE, target=SAuthor, nullable=False, cascade_delete=False
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
    r = SQLiteRenderer()

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
        assert self.r.sql_type(Column("x", complex)) == "TEXT"

    def test_sql_type_override_takes_precedence(self) -> None:
        col = Column("data", bytes, sql_type_override="JSONB")
        assert self.r.sql_type(col) == "JSONB"


# ---------------------------------------------------------------------------


class TestSQLiteRendererColumnDef:
    r = SQLiteRenderer()

    def test_pk_with_autoincrement(self) -> None:
        col = Column("id", int, primary_key=True, auto_increment=True)
        assert self.r._column_def(col) == "id INTEGER PRIMARY KEY AUTOINCREMENT"

    def test_pk_without_autoincrement(self) -> None:
        col = Column("id", int, primary_key=True, auto_increment=False)
        assert self.r._column_def(col) == "id INTEGER PRIMARY KEY"

    def test_required_not_null(self) -> None:
        col = Column("name", str)
        assert self.r._column_def(col) == "name TEXT NOT NULL"

    def test_nullable_no_constraint(self) -> None:
        col = Column("bio", str, nullable=True)
        assert self.r._column_def(col) == "bio TEXT"

    def test_unique(self) -> None:
        col = Column("email", str, unique=True)
        assert self.r._column_def(col) == "email TEXT NOT NULL UNIQUE"

    def test_sql_default(self) -> None:
        col = Column("status", str, sql_default="'active'")
        assert self.r._column_def(col) == "status TEXT NOT NULL DEFAULT 'active'"


# ---------------------------------------------------------------------------


class TestSQLiteRendererStatements:
    r = SQLiteRenderer()

    def test_create_table_basic(self) -> None:
        table = Table(
            name="post",
            columns=[
                Column("id", int, primary_key=True, auto_increment=True),
                Column("title", str),
            ],
        )
        sql = self.r.create_table(table)
        assert "CREATE TABLE IF NOT EXISTS post" in sql
        assert "id INTEGER PRIMARY KEY AUTOINCREMENT" in sql
        assert "title TEXT NOT NULL" in sql

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
        assert "CONSTRAINT fk_comment__post_id FOREIGN KEY (post_id)" in sql
        assert "REFERENCES post (id) ON DELETE CASCADE" in sql

    def test_drop_table(self) -> None:
        assert self.r.drop_table("post") == "DROP TABLE IF EXISTS post"

    def test_add_column(self) -> None:
        col = Column("body", str, nullable=True)
        assert self.r.add_column("post", col) == "ALTER TABLE post ADD COLUMN body TEXT"

    def test_drop_column(self) -> None:
        assert self.r.drop_column("post", "body") == "ALTER TABLE post DROP COLUMN body"

    def test_create_index_plain(self) -> None:
        idx = Index(name="idx_post__slug", columns=["slug"])
        sql = self.r.create_index("post", idx)
        assert sql == "CREATE INDEX IF NOT EXISTS idx_post__slug ON post (slug)"

    def test_create_index_unique(self) -> None:
        idx = Index(name="idx_post__slug", columns=["slug"], unique=True)
        sql = self.r.create_index("post", idx)
        assert sql == "CREATE UNIQUE INDEX IF NOT EXISTS idx_post__slug ON post (slug)"

    def test_drop_index(self) -> None:
        assert self.r.drop_index("idx_post__slug") == "DROP INDEX IF EXISTS idx_post__slug"


# ---------------------------------------------------------------------------


class TestDDLRendererDispatch:
    """Verify that DDLRenderer.render() dispatches every SchemaOp variant."""

    r = SQLiteRenderer()

    def test_render_create_table(self) -> None:
        op: SchemaOp = CreateTable(Table(name="t", columns=[Column("id", int)]))
        sql = self.r.render(op)
        assert "CREATE TABLE" in sql

    def test_render_drop_table(self) -> None:
        op: SchemaOp = DropTable(table_name="t")
        assert self.r.render(op) == "DROP TABLE IF EXISTS t"

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
    r = PostgresRenderer()

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
        assert self.r.sql_type(Column("x", list)) == "TEXT"

    def test_sql_type_override_takes_precedence(self) -> None:
        col = Column("data", bytes, sql_type_override="JSONB")
        assert self.r.sql_type(col) == "JSONB"


class TestPostgresRendererStatements:
    r = PostgresRenderer()

    def test_create_table_basic(self) -> None:
        table = Table(
            name="post",
            columns=[
                Column("id", int, primary_key=True, auto_increment=True),
                Column("title", str, nullable=False),
            ],
        )
        sql = self.r.create_table(table)
        assert "CREATE TABLE IF NOT EXISTS post" in sql
        assert "id SERIAL PRIMARY KEY" in sql
        assert "title TEXT NOT NULL" in sql

    def test_create_table_with_fk(self) -> None:
        table = Table(
            name="comment",
            columns=[Column("post_id", int, nullable=False)],
            foreign_keys=[ForeignKey("fk_comment__post_id", "post_id", "post")],
        )
        sql = self.r.create_table(table)
        assert "CONSTRAINT fk_comment__post_id FOREIGN KEY (post_id) REFERENCES post (id)" in sql

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
        assert self.r.drop_table("post") == "DROP TABLE IF EXISTS post"

    def test_add_column(self) -> None:
        col = Column("body", str, nullable=True)
        sql = self.r.add_column("post", col)
        assert sql == "ALTER TABLE post ADD COLUMN IF NOT EXISTS body TEXT"

    def test_add_column_not_null(self) -> None:
        col = Column("views", int, nullable=False)
        sql = self.r.add_column("post", col)
        assert "NOT NULL" in sql

    def test_drop_column(self) -> None:
        sql = self.r.drop_column("post", "body")
        assert sql == "ALTER TABLE post DROP COLUMN IF EXISTS body"

    def test_create_index(self) -> None:
        idx = Index("idx_post__title", ["title"])
        sql = self.r.create_index("post", idx)
        assert sql == "CREATE INDEX IF NOT EXISTS idx_post__title ON post (title)"

    def test_create_unique_index(self) -> None:
        idx = Index("unq_post__slug", ["slug"], unique=True)
        sql = self.r.create_index("post", idx)
        assert sql == "CREATE UNIQUE INDEX IF NOT EXISTS unq_post__slug ON post (slug)"

    def test_drop_index(self) -> None:
        assert self.r.drop_index("idx_post__title") == "DROP INDEX IF EXISTS idx_post__title"

    def test_render_drop_index_op(self) -> None:
        op: SchemaOp = DropIndex(table_name="post", index_name="idx_post__title")
        assert self.r.render(op) == "DROP INDEX IF EXISTS idx_post__title"


# ---------------------------------------------------------------------------
# MariaDBRenderer
# ---------------------------------------------------------------------------


class TestMariaDBRendererSqlType:
    r = MariaDBRenderer()

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
        assert self.r.sql_type(Column("x", list)) == "TEXT"

    def test_sql_type_override_takes_precedence(self) -> None:
        col = Column("data", bytes, sql_type_override="JSON")
        assert self.r.sql_type(col) == "JSON"


class TestMariaDBRendererStatements:
    r = MariaDBRenderer()

    def test_create_table_basic(self) -> None:
        table = Table(
            name="post",
            columns=[
                Column("id", int, primary_key=True, auto_increment=True),
                Column("title", str, nullable=False),
            ],
        )
        sql = self.r.create_table(table)
        assert "CREATE TABLE IF NOT EXISTS post" in sql
        assert "id INT AUTO_INCREMENT PRIMARY KEY" in sql
        assert "title TEXT NOT NULL" in sql

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
        assert "CONSTRAINT fk_comment__post_id FOREIGN KEY (post_id) REFERENCES post (id)" in sql

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
        assert self.r.drop_table("post") == "DROP TABLE IF EXISTS post"

    def test_add_column(self) -> None:
        col = Column("body", str, nullable=True)
        assert self.r.add_column("post", col) == "ALTER TABLE post ADD COLUMN body TEXT"

    def test_drop_column(self) -> None:
        assert self.r.drop_column("post", "body") == "ALTER TABLE post DROP COLUMN body"

    def test_create_index(self) -> None:
        idx = Index("idx_post__title", ["title"])
        assert self.r.create_index("post", idx) == "CREATE INDEX idx_post__title ON post (title)"

    def test_create_unique_index(self) -> None:
        idx = Index("unq_post__slug", ["slug"], unique=True)
        sql = self.r.create_index("post", idx)
        assert sql == "CREATE UNIQUE INDEX unq_post__slug ON post (slug)"

    def test_render_drop_index_includes_table(self) -> None:
        op: SchemaOp = DropIndex(table_name="post", index_name="idx_post__title")
        sql = self.r.render(op)
        assert sql == "DROP INDEX idx_post__title ON post"

    def test_render_delegates_non_drop_index(self) -> None:
        op: SchemaOp = DropTable(table_name="post")
        assert self.r.render(op) == "DROP TABLE IF EXISTS post"


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
        assert "slot, room" in sql

    def test_composite_index_ddl_sqlite(self) -> None:
        r = SQLiteRenderer()
        table = entity_to_table(LogEntry)
        idx = next(i for i in table.indexes if not i.unique)
        sql = r.create_index(table.name, idx)
        assert "UNIQUE" not in sql
        assert "source, level" in sql

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
        assert "slot, room" in sql

    def test_composite_key_ddl_mariadb(self) -> None:
        r = MariaDBRenderer()
        table = entity_to_table(Booking)
        idx = next(i for i in table.indexes if i.unique)
        sql = r.create_index(table.name, idx)
        assert "UNIQUE INDEX" in sql
        assert "slot, room" in sql

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
            amount: Req[decimal.Decimal] = FieldSpec(precision=10, scale=2)  # type: ignore[assignment]

        table = entity_to_table(PriceEntity)
        col = table.get_column("amount")
        assert col is not None
        assert col.precision == 10
        assert col.scale == 2

    def test_unsigned_pass_through(self) -> None:
        class CountEntity(Entity):
            qty: Req[int] = FieldSpec(unsigned=True)  # type: ignore[assignment]

        table = entity_to_table(CountEntity)
        col = table.get_column("qty")
        assert col is not None
        assert col.unsigned is True

    def test_precision_scale_unsigned_in_ddl(self) -> None:
        class InvoiceItem(Entity):
            price: Req[decimal.Decimal] = FieldSpec(precision=8, scale=2)  # type: ignore[assignment]

        table = entity_to_table(InvoiceItem)
        r = MariaDBRenderer()
        sql = r.create_table(table)
        assert "DECIMAL(8, 2)" in sql

    def test_unsigned_int_in_ddl(self) -> None:
        class Stock(Entity):
            quantity: Req[int] = FieldSpec(unsigned=True)  # type: ignore[assignment]

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
            count: Req[int] = FieldSpec(size=16)  # type: ignore[assignment]

        table = entity_to_table(SmallIntEntity)
        col = table.get_column("count")
        assert col is not None
        assert col.size == 16

    def test_size_in_mariadb_ddl(self) -> None:
        class BigEntity(Entity):
            big_id: Req[int] = FieldSpec(size=64)  # type: ignore[assignment]

        sql = MariaDBRenderer().create_table(entity_to_table(BigEntity))
        assert "BIGINT" in sql

    def test_size_in_postgres_ddl(self) -> None:
        class ByteEntity(Entity):
            flags: Req[int] = FieldSpec(size=8)  # type: ignore[assignment]

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
        assert "USING ivfflat (embedding)" in sql
        assert "WITH" not in sql

    def test_postgres_btree_explicit_stays_standard(self) -> None:
        idx = Index("idx_title", ["title"], method="btree")
        sql = PostgresRenderer().create_index("post", idx)
        assert "USING" not in sql
        assert sql == "CREATE INDEX IF NOT EXISTS idx_title ON post (title)"

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
        assert stmt == "ALTER TABLE user ALTER COLUMN age TYPE TEXT"

    def test_postgres_alter_column_type_not_null(self) -> None:
        col = Column("age", int, nullable=False)
        stmt = PostgresRenderer().alter_column_type("post", col)
        assert stmt == "ALTER TABLE post ALTER COLUMN age TYPE INTEGER NOT NULL"

    def test_postgres_alter_column_type_varchar(self) -> None:
        col = Column("email", str, max_len=255, nullable=False)
        stmt = PostgresRenderer().alter_column_type("account", col)
        assert "ALTER COLUMN email TYPE VARCHAR(255) NOT NULL" in stmt

    def test_mariadb_modify_column_nullable(self) -> None:
        col = Column("age", str, nullable=True)
        stmt = MariaDBRenderer().alter_column_type("user", col)
        assert stmt == "ALTER TABLE user MODIFY COLUMN age TEXT"

    def test_mariadb_modify_column_not_null(self) -> None:
        col = Column("age", int, nullable=False)
        stmt = MariaDBRenderer().alter_column_type("post", col)
        assert stmt == "ALTER TABLE post MODIFY COLUMN age INT NOT NULL"

    def test_mariadb_modify_column_varchar(self) -> None:
        col = Column("name", str, max_len=80, nullable=False)
        stmt = MariaDBRenderer().alter_column_type("profile", col)
        assert "MODIFY COLUMN name VARCHAR(80) NOT NULL" in stmt


class TestAlterColumnTypeRenderDispatch:
    """DDLRenderer.render() dispatches AlterColumnType correctly for each renderer."""

    def test_sqlite_render_dispatch(self) -> None:
        op: SchemaOp = AlterColumnType(table_name="t", column=Column("x", str))
        result = SQLiteRenderer().render(op)
        assert "-- SQLite:" in result

    def test_postgres_render_dispatch(self) -> None:
        op: SchemaOp = AlterColumnType(table_name="t", column=Column("x", int, nullable=False))
        result = PostgresRenderer().render(op)
        assert result == "ALTER TABLE t ALTER COLUMN x TYPE INTEGER NOT NULL"

    def test_mariadb_render_dispatch(self) -> None:
        op: SchemaOp = AlterColumnType(table_name="t", column=Column("x", int, nullable=False))
        result = MariaDBRenderer().render(op)
        assert result == "ALTER TABLE t MODIFY COLUMN x INT NOT NULL"
