"""Tests for entity introspection, field aliases, and lifecycle hooks."""

from __future__ import annotations

import pytest

from nextorm import (
    PK,
    Entity,
    Local,
    Opt,
    Req,
    Set,
    Single,
    flush,
)
from nextorm.database import Database as _Database
from nextorm.entity import (
    _LAZY_SENTINEL,
    FieldDescriptor,
    FieldInfo,
    LocalDescriptor,
    RelationInfo,
    _entity_registry,
    _matches_entity,
    _resolve_entity_target,
    _target_name,
)
from nextorm.fields import FieldSpec, LongStr, RelationKind
from nextorm.fields import PrimaryKey as _PrimaryKey
from nextorm.session import db_session
from nextorm.session import db_session as _db_session

# ---------------------------------------------------------------------------
# Helper entities defined at module scope
# ---------------------------------------------------------------------------


class Tag(Entity):
    name: Req[str]


class Product(Entity):
    name: Req[str]
    price: Req[float]
    description: Opt[str]
    tags: Set[Tag]
    comments: Set["Comment"]  # noqa: UP037


class Comment(Entity):
    body: Req[str]
    product: Single[Product]


class Article(Entity):
    custom_id: PK[int]
    title: Req[str]


class OrderItem(Entity):
    quantity: Req[int]
    _cached_total: Local[float]

    def after_load(self) -> None:
        self._cached_total = 0.0

    def after_insert(self) -> None:
        self._cached_total = 0.0


# ---------------------------------------------------------------------------
# Field alias tests
# ---------------------------------------------------------------------------


class TestFieldAliases:
    """Verify that type aliases produce the correct FieldSpec / RelationSpec in _fields_."""

    def test_req_field_is_non_nullable(self) -> None:
        fi = Product._fields_["name"]
        assert fi.py_type is str
        assert not fi.spec.nullable
        assert not fi.spec.primary_key

    def test_opt_field_is_nullable(self) -> None:
        fi = Product._fields_["description"]
        assert fi.py_type is str
        assert fi.spec.nullable is True

    def test_pk_field_has_primary_key_spec(self) -> None:
        fi = Article._fields_["custom_id"]
        assert fi.py_type is int
        assert fi.spec.primary_key is True
        assert fi.spec.auto is True

    def test_onetomany_creates_relation_with_correct_kind(self) -> None:
        class Post(Entity):
            comments: Set[Comment]

        assert "comments" in Post._relations_
        assert Post._relations_["comments"].spec.kind == RelationKind.SET

    def test_manytomany_creates_relation_with_correct_kind(self) -> None:
        assert "tags" in Product._relations_
        assert Product._relations_["tags"].spec.kind == RelationKind.SET

    def test_manytoone_creates_relation_with_correct_kind(self) -> None:
        assert "product" in Comment._relations_
        assert Comment._relations_["product"].spec.kind == RelationKind.SINGLE

    def test_manytoone_optional_sets_nullable(self) -> None:
        class WithOpt(Entity):
            owner: Single[Product | None]

        ri = WithOpt._relations_["owner"]
        assert ri.spec.nullable is True
        assert ri.spec.target is Product

    def test_manytoone_required_not_nullable(self) -> None:
        class WithReq(Entity):
            owner: Single[Product]

        ri = WithReq._relations_["owner"]
        assert ri.spec.nullable is False

    def test_virtual_creates_virtual_entry(self) -> None:
        assert "_cached_total" in OrderItem._locals_

    def test_has_default_false(self) -> None:
        spec = FieldSpec()
        assert spec.has_default is False

    def test_has_default_true(self) -> None:
        spec = FieldSpec(default="hello")
        assert spec.has_default is True


# ---------------------------------------------------------------------------
# EntityMeta introspection tests
# ---------------------------------------------------------------------------


class TestEntityMeta:
    def test_fields_dict_populated(self) -> None:
        assert "name" in Product._fields_
        assert "price" in Product._fields_

    def test_field_info_type(self) -> None:
        fi = Product._fields_["name"]
        assert isinstance(fi, FieldInfo)
        assert fi.py_type is str
        assert not fi.spec.nullable

    def test_optional_field_nullable(self) -> None:
        fi = Product._fields_["description"]
        assert fi.spec.nullable is True

    def test_relations_dict_populated(self) -> None:
        assert "tags" in Product._relations_
        ri = Product._relations_["tags"]
        assert isinstance(ri, RelationInfo)
        assert ri.spec.kind == RelationKind.SET

    def test_virtuals_set_populated(self) -> None:
        assert "_cached_total" in OrderItem._locals_

    def test_virtual_not_in_fields(self) -> None:
        assert "_cached_total" not in OrderItem._fields_

    def test_virtual_not_in_relations(self) -> None:
        assert "_cached_total" not in OrderItem._relations_

    def test_auto_pk_added_when_missing(self) -> None:
        # Product has no explicit PK — auto `id` should be added
        assert "id" in Product._fields_
        assert Product._fields_["id"].spec.primary_key is True
        assert Product._fields_["id"].spec.auto is True
        assert Product._pk_field_ == "id"

    def test_explicit_pk_used(self) -> None:
        assert Article._pk_field_ == "custom_id"
        assert "id" not in Article._fields_

    def test_pk_field_attribute(self) -> None:
        assert Product._pk_field_ == "id"

    def test_field_info_repr(self) -> None:
        fi = Product._fields_["name"]
        r = repr(fi)
        assert "FieldInfo" in r
        assert "name" in r
        assert "str" in r

    def test_relation_info_repr(self) -> None:
        ri = Product._relations_["tags"]
        r = repr(ri)
        assert "RelationInfo" in r
        assert "tags" in r

    def test_mixin_entity_skips_mixin_in_mro(self) -> None:
        """A mixin (non-EntityMeta) class in the MRO must be silently skipped."""

        class Mixin:
            def helper(self) -> str:
                return "mixin"

        class WithMixin(Entity, Mixin):
            name: Req[str]

        assert "name" in WithMixin._fields_
        assert WithMixin(name="x").helper() == "mixin"

    def test_dunder_annotation_skipped(self) -> None:
        """Annotations whose names start with __ are silently skipped."""

        class WithDunder(Entity):
            __proto__: int
            name: Req[str]

        assert "name" in WithDunder._fields_
        assert "__proto__" not in WithDunder._fields_

    def test_entity_registered_in_registry(self) -> None:
        assert Product in _entity_registry
        assert Tag in _entity_registry

    def test_plain_annotation_is_ignored(self) -> None:
        """A bare Python annotation (no nextorm alias) is silently skipped."""

        class WithPlain(Entity):
            score: int  # plain int — no Req/Opt/PK wrapper → ignored by EntityMeta
            name: Req[str]

        assert "score" not in WithPlain._fields_
        assert "score" not in WithPlain._relations_
        assert "score" not in WithPlain._locals_
        assert "name" in WithPlain._fields_


# ---------------------------------------------------------------------------
# Descriptor tests
# ---------------------------------------------------------------------------


class TestDescriptors:
    def test_field_descriptor_on_class(self) -> None:
        assert isinstance(Product.__dict__["name"], FieldDescriptor)

    def test_field_descriptor_class_attribute_returns_column_expr(self) -> None:
        # Accessing via the class (not an instance) now returns a ColumnExpr
        # for use in query predicates (e.g. Product.name == "x")
        from nextorm.expr import ColumnExpr

        result = Product.name
        assert isinstance(result, ColumnExpr)
        assert result.field_name == "name"

    def test_virtual_descriptor_class_attribute_returns_descriptor(self) -> None:
        descriptor = OrderItem._cached_total
        assert isinstance(descriptor, LocalDescriptor)

    def test_field_read_write(self) -> None:
        p = Product.__new__(Product)
        assert p.name is None  # not set yet
        p.name = "Widget"
        assert p.name == "Widget"

    def test_virtual_descriptor_on_class(self) -> None:
        assert isinstance(OrderItem.__dict__["_cached_total"], LocalDescriptor)

    def test_virtual_raises_before_set(self) -> None:
        item = OrderItem.__new__(OrderItem)
        with pytest.raises(AttributeError, match="has not been set yet"):
            _ = item._cached_total

    def test_virtual_read_write(self) -> None:
        item = OrderItem.__new__(OrderItem)
        item._cached_total = 9.99
        assert item._cached_total == 9.99

    def test_virtual_delete(self) -> None:
        item = OrderItem.__new__(OrderItem)
        item._cached_total = 5.0
        del item._cached_total
        with pytest.raises(AttributeError):
            _ = item._cached_total

    def test_field_descriptor_delete(self) -> None:
        p = Product.__new__(Product)
        p.name = "before"
        del p.name
        assert p.name is None  # returns None after deletion

    def test_virtual_not_shared_between_instances(self) -> None:
        a = OrderItem.__new__(OrderItem)
        b = OrderItem.__new__(OrderItem)
        a._cached_total = 1.0
        b._cached_total = 2.0
        assert a._cached_total == 1.0
        assert b._cached_total == 2.0


# ---------------------------------------------------------------------------
# Entity.__init__ and __repr__ tests
# ---------------------------------------------------------------------------


class TestEntityInit:
    def test_init_sets_fields(self) -> None:
        p = Product(name="Widget", price=9.99)
        assert p.name == "Widget"
        assert p.price == 9.99

    def test_repr(self) -> None:
        p = Product.__new__(Product)
        p.id = 42
        assert "Product" in repr(p)
        assert "42" in repr(p)

    def test_repr_no_pk_fields(self) -> None:
        p = Product.__new__(Product)
        orig_pk_fields = Product._pk_fields_
        Product._pk_fields_ = ()
        try:
            r = repr(p)
        finally:
            Product._pk_fields_ = orig_pk_fields
        assert r == "Product()"

    def test_field_default_applied_when_not_provided(self) -> None:
        class Scored(Entity):
            name: Req[str]
            score: Req[int] = FieldSpec(default=0)  # type: ignore[assignment]

        s = Scored(name="test")
        assert s.score == 0

    def test_field_default_callable_applied(self) -> None:
        class Tagged(Entity):
            label: Req[str] = FieldSpec(default=lambda: "default_label")  # type: ignore[assignment]

        t = Tagged()
        assert t.label == "default_label"

    def test_explicit_kwarg_overrides_default(self) -> None:
        class Valued(Entity):
            amount: Req[int] = FieldSpec(default=42)  # type: ignore[assignment]

        v = Valued(amount=99)
        assert v.amount == 99


# ---------------------------------------------------------------------------
# Lifecycle hook tests
# ---------------------------------------------------------------------------


class TestLifecycleHooks:
    def test_after_load_initialises_virtual(self) -> None:
        item = OrderItem.__new__(OrderItem)
        item.after_load()
        assert item._cached_total == 0.0

    def test_after_insert_initialises_virtual(self) -> None:
        item = OrderItem.__new__(OrderItem)
        item.after_insert()
        assert item._cached_total == 0.0

    def test_hooks_are_callable_on_base_entity(self) -> None:
        # Default hooks must be no-ops and not raise
        p = Product.__new__(Product)
        p.after_load()
        p.before_insert()
        p.after_insert()
        p.before_update()
        p.after_update()
        p.before_delete()
        p.after_delete()

    def test_hook_override_pattern(self) -> None:
        """Verify the pattern recommended in the plan works end-to-end."""

        class Counter(Entity):
            value: Req[int]
            _load_count: Local[int]

            def after_load(self) -> None:
                try:
                    self._load_count += 1
                except AttributeError:
                    self._load_count = 1

        obj = Counter.__new__(Counter)
        assert "_load_count" in Counter._locals_
        obj.after_load()
        assert obj._load_count == 1
        obj.after_load()
        assert obj._load_count == 2

    def test_before_and_after_update_delete_callable(self) -> None:
        p = Product.__new__(Product)
        p.before_update()
        p.after_update()
        p.before_delete()
        p.after_delete()


# ---------------------------------------------------------------------------
# _target_name / _resolve_entity_target / _matches_entity helpers
# ---------------------------------------------------------------------------


class TestTargetName:
    def test_string(self) -> None:
        assert _target_name("MyModel") == "mymodel"

    def test_forwardref(self) -> None:
        import typing  # noqa: PLC0415

        assert _target_name(typing.ForwardRef("MyModel")) == "mymodel"

    def test_type(self) -> None:
        assert _target_name(Tag) == "tag"

    def test_unknown_returns_none(self) -> None:
        assert _target_name(42) is None


class TestResolveEntityTarget:
    def test_returns_none_when_name_is_none(self) -> None:
        # Pass something _target_name can't handle → name becomes None → returns None
        result = _resolve_entity_target(42)
        assert result is None


class TestMatchesEntity:
    def test_returns_false_for_non_matching_target(self) -> None:
        # target is not Tag, not a matching string, not a matching ForwardRef
        assert _matches_entity("other", Tag) is False

    def test_returns_false_name_is_none(self) -> None:
        # 42 → _target_name returns None → name is None → returns False
        assert _matches_entity(42, Tag) is False


# ---------------------------------------------------------------------------
# Entity instance helpers: get_pk / set / delete / to_dict
# ---------------------------------------------------------------------------


class _SimpleEntity(Entity):
    name: Req[str]
    score: Opt[int]


class TestGetPk:
    def test_returns_pk_after_save(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[_SimpleEntity])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True)
        with db_session:
            e = _SimpleEntity(name="x", score=1)
        assert e.get_pk() == e.id
        db.close()

    def test_returns_none_before_save(self) -> None:
        e = _SimpleEntity(name="x")
        assert e.get_pk() is None

    def test_entity_without_pk_returns_none(self) -> None:
        # Entity base class has no _pk_field_ resolved (it has the dummy id)
        # so test via a freshly unsaved entity
        e = _SimpleEntity(name="x")
        e.__class__._pk_field_ = None
        e.__class__._pk_fields_ = ()
        assert e.get_pk() is None
        e.__class__._pk_field_ = "id"  # restore
        e.__class__._pk_fields_ = ("id",)  # restore


class TestSetMethod:
    def test_sets_multiple_fields(self) -> None:
        e = _SimpleEntity(name="initial", score=0)
        e.set(name="updated", score=42)
        assert e.name == "updated"
        assert e.score == 42

    def test_set_empty_is_noop(self) -> None:
        e = _SimpleEntity(name="x", score=1)
        e.set()
        assert e.name == "x"
        assert e.score == 1


class TestDeleteMethod:
    def test_delete_removes_entity(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[_SimpleEntity])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True)
        with db_session:
            e = _SimpleEntity(name="to-delete", score=5)
        pk = e.id
        e.delete()
        remaining = db.select(_SimpleEntity).filter(_SimpleEntity.id == pk).fetch_all()
        assert remaining == []
        db.close()

    def test_delete_without_db_context_raises(self) -> None:
        e = _SimpleEntity(name="orphan")
        with pytest.raises(RuntimeError, match="_db_ not set"):
            e.delete()


class TestToDict:
    def test_all_fields_included_by_default(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[_SimpleEntity])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True)
        with db_session:
            e = _SimpleEntity(name="alice", score=3)
        d = e.to_dict()
        assert d["name"] == "alice"
        assert d["score"] == 3
        assert "id" in d
        db.close()

    def test_only_restricts_fields(self) -> None:
        e = _SimpleEntity(name="bob", score=7)
        d = e.to_dict(only=["name"])
        assert list(d.keys()) == ["name"]

    def test_exclude_removes_fields(self) -> None:
        e = _SimpleEntity(name="carol", score=9)
        d = e.to_dict(exclude=["score"])
        assert "score" not in d
        assert "name" in d

    def test_with_collections_includes_set_relation(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[Tag, Product, Comment])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)
        with db_session:
            p = Product(name="Book", price=10.0)
            db.flush()  # p.id set
            c = Comment(body="great book")
            c.product = p
        # Prefetch the comments before calling to_dict
        result = db.select(Product).prefetch(Product.comments).filter(Product.id == p.id).fetch_one()
        assert result is not None
        d = result.to_dict(with_collections=True)
        assert "comments" in d
        assert any(item["body"] == "great book" for item in d["comments"])
        db.close()

    def test_with_collections_false_omits_sets(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[Tag, Product, Comment])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)
        with db_session:
            p = Product(name="Book", price=10.0)
        d = p.to_dict(with_collections=False)
        assert "tags" not in d
        db.close()

    def test_with_collections_only_excludes_other_sets(self) -> None:
        """only= filter inside with_collections loop skips non-matching relations."""
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[Tag, Product, Comment])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)
        with db_session:
            p = Product(name="Book", price=10.0)
        # only=["id"] means collection names "tags"/"comments" are not in only → continue
        d = p.to_dict(only=["id"], with_collections=True)
        assert "tags" not in d
        assert "comments" not in d
        db.close()

    def test_with_collections_exclude_skips_named_set(self) -> None:
        """exclude= filter inside with_collections loop skips excluded relations."""
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[Tag, Product, Comment])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)
        with db_session:
            p = Product(name="Book", price=10.0)
        d = p.to_dict(exclude=["tags"], with_collections=True)
        assert "tags" not in d
        assert "comments" in d
        db.close()

    def test_with_collections_manytoone_not_included(self) -> None:
        """ManyToOne relations are skipped in with_collections mode."""
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[Tag, Product, Comment])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)
        with db_session:
            p = Product(name="Book", price=10.0)
            db.flush()  # p.id set
            c = Comment(body="hi")
            c.product = p
        # Comment has Single[Product] — should not appear in with_collections dict
        d = c.to_dict(with_collections=True)
        assert "product" not in d
        db.close()


# ---------------------------------------------------------------------------
# Module-level entities for lazy / to_dict tests
# ---------------------------------------------------------------------------


class _LazyPage(Entity):
    """Entity with an explicit lazy field for to_dict and EntityMeta tests."""

    title: Req[str]
    body: Req[str] = FieldSpec(lazy=True)  # type: ignore[assignment]


class _LongStrDoc(Entity):
    """Entity whose body field uses LongStr (auto-lazy)."""

    title: Req[str]
    body: Req[LongStr]


# ---------------------------------------------------------------------------
# _table_ override  (module-level entities for FK cross-ref test)
# ---------------------------------------------------------------------------


class ParentCustomTable(Entity):
    _table_ = "parent_custom_t"
    label: Req[str]


class ChildCustomTable(Entity):
    parent: Single[ParentCustomTable]


class TestTableOverride:
    def test_default_table_name_is_class_name_lower(self) -> None:
        class MyWidget(Entity):
            label: Req[str]

        assert MyWidget._table_name_ == "mywidget"

    def test_custom_table_name_set_via_table_(self) -> None:
        class PricedItem(Entity):
            _table_ = "items"
            price: Req[float]

        assert PricedItem._table_name_ == "items"

    def test_custom_table_name_used_in_schema(self) -> None:
        from nextorm.schema import build_schema  # noqa: PLC0415

        class OverriddenEntity(Entity):
            _table_ = "my_custom_table"
            value: Req[int]

        tables = build_schema([OverriddenEntity])
        assert "my_custom_table" in tables
        assert "overriddenentity" not in tables

    def test_custom_table_used_in_database(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        class StoredItem(Entity):
            _table_ = "stored_items"
            name: Req[str]

        db = Database(entities=[StoredItem])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True)
        with db_session:
            StoredItem(name="widget")
        results = db.select(StoredItem).fetch_all()
        assert len(results) == 1
        assert results[0].name == "widget"
        db.close()

    def test_fk_column_ref_table_uses_custom_name(self) -> None:
        from nextorm.schema import build_schema  # noqa: PLC0415

        tables = build_schema([ParentCustomTable, ChildCustomTable])
        child_table = tables["childcustomtable"]
        fk = next(f for f in child_table.foreign_keys if f.column == "parent_id")
        assert fk.ref_table == "parent_custom_t"


# ---------------------------------------------------------------------------
# Lazy field & LongStr auto-lazy — EntityMeta guards and FieldSpec behaviour
# ---------------------------------------------------------------------------


class TestLazyFieldMeta:
    def test_lazy_pk_guard_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="primary key"):

            class _BadEntity(Entity):  # type: ignore[unused-ignore]
                pk: PK[int] = FieldSpec(primary_key=True, lazy=True)  # type: ignore[assignment]

    def test_lazy_sentinel_is_exported(self) -> None:
        from nextorm.entity import _LAZY_SENTINEL as _S  # noqa: PLC0415

        assert _S is _LAZY_SENTINEL

    def test_explicit_lazy_field_has_spec_lazy_true(self) -> None:
        fi = _LazyPage._fields_["body"]
        assert fi.spec.lazy is True

    def test_longstr_field_auto_lazy(self) -> None:
        """LongStr field with no explicit FieldSpec gets lazy=True automatically."""
        fi = _LongStrDoc._fields_["body"]
        assert fi.spec.lazy is True

    def test_longstr_explicit_fieldspec_overrides_auto_lazy(self) -> None:
        """Providing an explicit FieldSpec opts the user in to controlling lazy."""

        class _EagerLong(Entity):
            text: Req[LongStr] = FieldSpec(lazy=False)  # type: ignore[assignment]

        fi = _EagerLong._fields_["text"]
        assert fi.spec.lazy is False

    def test_non_lazy_field_has_spec_lazy_false(self) -> None:
        fi = _LazyPage._fields_["title"]
        assert fi.spec.lazy is False


# ---------------------------------------------------------------------------
# to_dict — with_lazy and related_objects
# ---------------------------------------------------------------------------


class TestToDictLazy:
    def test_with_lazy_false_excludes_unloaded_lazy_field(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[_LazyPage])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True)
        with db_session:
            page = _LazyPage(title="Hello")
            page.body = "Content"

        loaded = db.select(_LazyPage).filter(_LazyPage.id == page.id).fetch_one()
        assert loaded is not None
        # body is lazy and not yet loaded — to_dict(with_lazy=False) should omit it
        d = loaded.to_dict(with_lazy=False)
        assert "body" not in d
        assert d["title"] == "Hello"
        db.close()

    def test_with_lazy_true_includes_lazy_field(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[_LazyPage])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True)
        with db_session:
            page = _LazyPage(title="World")
            page.body = "Body text"

        loaded = db.select(_LazyPage).filter(_LazyPage.id == page.id).fetch_one()
        assert loaded is not None
        d = loaded.to_dict(with_lazy=True)
        assert d["body"] == "Body text"
        assert d["title"] == "World"
        db.close()

    def test_with_lazy_false_includes_already_loaded_lazy_field(self) -> None:
        """If the lazy field is already loaded (sentinel replaced), include it."""
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[_LazyPage])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True)
        with db_session:
            page = _LazyPage(title="Pre-loaded")
            page.body = "Loaded body"

        loaded = db.select(_LazyPage).filter(_LazyPage.id == page.id).fetch_one()
        assert loaded is not None
        _ = loaded.body  # trigger lazy load → sentinel replaced
        d = loaded.to_dict(with_lazy=False)
        # field is now loaded → included even with with_lazy=False
        assert "body" in d
        assert d["body"] == "Loaded body"
        db.close()


class TestToDictRelatedObjects:
    def test_related_objects_true_includes_loaded_single(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[Tag, Product, Comment])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)

        with db_session:
            p = Product(name="Gadget", price=9.99)
            db.flush()  # p.id set
            c = Comment(body="cool")
            c.product = p

        loaded_c = db.select(Comment).filter(Comment.id == c.id).fetch_one()
        assert loaded_c is not None
        # Access the relation to populate the _product_obj cache
        _ = loaded_c.product
        d = loaded_c.to_dict(related_objects=True)
        assert "product" in d
        assert isinstance(d["product"], dict)
        assert d["product"]["name"] == "Gadget"

    def test_related_objects_true_falls_back_to_fk_id_when_not_loaded(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[Tag, Product, Comment])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)

        with db_session:
            p = Product(name="Widget", price=4.99)
            db.flush()  # p.id set
            c = Comment(body="nice")
            c.product = p

        loaded_c = db.select(Comment).filter(Comment.id == c.id).fetch_one()
        assert loaded_c is not None
        # Do NOT access .product so _product_obj is not cached
        d = loaded_c.to_dict(related_objects=True)
        assert "product" not in d
        assert d.get("product_id") == p.id

    def test_related_objects_false_excludes_single_relation(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[Tag, Product, Comment])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)

        with db_session:
            p = Product(name="Thing", price=1.99)
            db.flush()  # p.id set
            c = Comment(body="ok")
            c.product = p

        loaded_c = db.select(Comment).filter(Comment.id == c.id).fetch_one()
        assert loaded_c is not None
        _ = loaded_c.product  # load it into cache
        d = loaded_c.to_dict(related_objects=False)
        # related_objects=False → Single relations not included
        assert "product" not in d
        assert "product_id" not in d

    def test_related_objects_only_filter_applies(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[Tag, Product, Comment])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)

        with db_session:
            p = Product(name="Filter", price=2.99)
            db.flush()  # p.id set
            c = Comment(body="filtered")
            c.product = p

        loaded_c = db.select(Comment).filter(Comment.id == c.id).fetch_one()
        assert loaded_c is not None
        _ = loaded_c.product  # load into cache
        # only=["body"] means "product" is not in only → skipped
        d = loaded_c.to_dict(only=["body"], related_objects=True)
        assert "product" not in d
        assert d.get("body") == "filtered"

    def test_related_objects_exclude_filter_applies(self) -> None:
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[Tag, Product, Comment])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)

        with db_session:
            p = Product(name="Excl", price=3.99)
            db.flush()  # p.id set
            c = Comment(body="excluded-test")
            c.product = p

        loaded_c = db.select(Comment).filter(Comment.id == c.id).fetch_one()
        assert loaded_c is not None
        _ = loaded_c.product  # load into cache
        # exclude=["product"] → relation skipped
        d = loaded_c.to_dict(exclude=["product"], related_objects=True)
        assert "product" not in d

    def test_related_objects_nullable_single_not_set_skips_fk_id(self) -> None:
        """When the FK is None (nullable relation not set), no key is added."""
        from nextorm.database import Database  # noqa: PLC0415

        db = Database(entities=[Tag, Product, Comment])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)

        # Save a product and load a comment; then manually clear the FK to None
        # to exercise the fk_id is None branch in to_dict(related_objects=True).
        with db_session:
            p = Product(name="Ghost", price=0.0)
            db.flush()  # p.id set
            c = Comment(body="no fk")
            c.product = p

        loaded_c = db.select(Comment).filter(Comment.id == c.id).fetch_one()
        assert loaded_c is not None
        # Manually wipe the FK so fk_id is None
        vars(loaded_c).pop("_product_id", None)
        vars(loaded_c).pop("_product_obj", None)
        d = loaded_c.to_dict(related_objects=True)
        # fk_id is None → neither "product" nor "product_id" added
        assert "product" not in d
        assert "product_id" not in d
        db.close()


# ---------------------------------------------------------------------------
# Entity[pk], Entity.get(), Entity.exists()
# ---------------------------------------------------------------------------

# Module-level DB so _find_db_for_entity can discover it via sys.modules scan.


class _LookupUser(Entity):
    name: Req[str]
    age: Req[int]


class _MultiPK(Entity):
    _table_name_ = "_multi_lookup_entity"
    a: Req[int]
    b: Req[int]
    _pk_ = _PrimaryKey("a", "b")


_lookup_db = _Database(entities=[_LookupUser, _MultiPK])
_lookup_db.bind("sqlite", ":memory:")
_lookup_db.generate_mapping(create_tables=True)


def test_entity_getitem_returns_entity() -> None:
    """User[pk] retrieves the row with the given primary key."""
    with _db_session:
        u = _LookupUser(name="alice", age=30)
        flush()
        pk = u.id

    with _db_session:
        result = _LookupUser[pk]  # type: ignore[type-arg, valid-type]
        assert result.name == "alice"


def test_entity_getitem_raises_key_error_for_missing_pk() -> None:
    """User[pk] raises KeyError when the PK does not exist."""
    with _db_session, pytest.raises(KeyError):
        _LookupUser[9999]


def test_entity_getitem_single_pk_tuple_wrapping() -> None:
    """Entity[(pk,)] is accepted and unwrapped for single-PK entities."""
    with _db_session:
        u = _LookupUser(name="tuple-wrap", age=5)
        flush()
        pk = u.id

    with _db_session:
        result = _LookupUser[(pk,)]  # type: ignore[type-arg, valid-type]
        assert result.name == "tuple-wrap"


def test_entity_getitem_single_pk_tuple_wrong_length_raises_value_error() -> None:
    """Entity[(a, b)] raises ValueError for a single-PK entity."""
    with _db_session, pytest.raises(ValueError, match="single primary key"):
        _LookupUser[(1, 2)]

    """Entity.__getitem__ raises ValueError when the wrong number of PK values is given."""
    with pytest.raises(ValueError, match="2 PK fields"):
        _MultiPK[1]


def test_entity_getitem_composite_pk_returns_entity() -> None:
    """Entity.__getitem__ with a tuple retrieves the composite-PK row."""
    with _db_session:
        m = _MultiPK(a=10, b=20)
        _lookup_db.save(m)

    with _db_session:
        result = _MultiPK[10, 20]  # type: ignore[type-arg, valid-type]
        assert result.a == 10
        assert result.b == 20


def test_entity_getitem_composite_pk_raises_key_error_for_missing() -> None:
    """Entity.__getitem__ raises KeyError for a composite PK that does not exist."""
    with _db_session, pytest.raises(KeyError):
        _MultiPK[99, 99]


def test_entity_get_returns_matching_entity() -> None:
    """Entity.get(**kwargs) returns the first matching entity."""
    with _db_session:
        _LookupUser(name="bob", age=25)

    with _db_session:
        u = _LookupUser.get(name="bob")
        assert u is not None
        assert u.age == 25


def test_entity_get_returns_none_when_not_found() -> None:
    """Entity.get(**kwargs) returns None when no row matches."""
    with _db_session:
        result = _LookupUser.get(name="nobody")
        assert result is None


def test_entity_exists_true() -> None:
    """Entity.exists(**kwargs) returns True when a matching row exists."""
    with _db_session:
        _LookupUser(name="carol", age=20)

    with _db_session:
        assert _LookupUser.exists(name="carol") is True


def test_entity_exists_false() -> None:
    """Entity.exists(**kwargs) returns False when no matching row exists."""
    with _db_session:
        assert _LookupUser.exists(name="ghost") is False


def test_find_db_raises_when_no_mapping() -> None:
    """_find_db_for_entity raises RuntimeError when entity has no mapped DB."""
    from nextorm.entity import _find_db_for_entity as _fdb

    class _Unmapped(Entity):
        _table_name_ = "_unmapped_xyz_unique"
        x: Req[int]

    with pytest.raises(RuntimeError, match="Cannot find a mapped Database"):
        _fdb(_Unmapped)


# ---------------------------------------------------------------------------
# Entity.select()
# ---------------------------------------------------------------------------


def test_entity_select_returns_queryset() -> None:
    """Entity.select() returns a QuerySet for the entity."""
    with _db_session:
        _LookupUser(name="dave", age=40)

    with _db_session:
        results = _LookupUser.select().filter(_LookupUser.name == "dave").fetch_all()
        assert len(results) == 1
        assert results[0].name == "dave"


def test_entity_select_raises_for_unmapped_entity() -> None:
    """Entity.select() raises RuntimeError when the entity has no mapped DB."""

    class _UnmappedSel(Entity):
        _table_name_ = "_unmapped_sel_xyz"
        x: Req[int]

    with pytest.raises(RuntimeError, match="Cannot find a mapped Database"):
        _UnmappedSel.select()


# ---------------------------------------------------------------------------
# Entity.aselect() / Entity.aget() — sync DB raises RuntimeError
# ---------------------------------------------------------------------------


def test_entity_aselect_raises_for_sync_db() -> None:
    """Entity.aselect() raises RuntimeError when the entity is on a sync Database."""
    with pytest.raises(RuntimeError, match="sync Database"):
        _LookupUser.aselect()


@pytest.mark.asyncio
async def test_entity_aget_raises_for_sync_db() -> None:
    """Entity.aget() raises RuntimeError when the entity is on a sync Database."""
    with pytest.raises(RuntimeError, match="sync Database"):
        await _LookupUser.aget(name="alice")


# ---------------------------------------------------------------------------
# Entity.aselect() / Entity.aget() — async DB
# ---------------------------------------------------------------------------


class _AsyncLookupUser(Entity):
    _table_name_ = "_async_lookup_user"
    name: Req[str]
    age: Req[int]


@pytest.mark.asyncio
async def test_entity_aselect_returns_async_queryset() -> None:
    """Entity.aselect() returns an AsyncQuerySet for the entity."""
    from nextorm.async_database import AsyncDatabase

    db = AsyncDatabase(entities=[_AsyncLookupUser])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)

    from nextorm.session import db_session as _sess

    async with _sess:
        u = _AsyncLookupUser(name="eve", age=22)
        await db.asave(u)

    async with _sess:
        results = (
            await _AsyncLookupUser.aselect().filter(_AsyncLookupUser.name == "eve").fetch_all()
        )
        assert len(results) == 1
        assert results[0].name == "eve"

    await db.close()


@pytest.mark.asyncio
async def test_entity_aget_returns_matching_entity() -> None:
    """Entity.aget(**kwargs) returns the first matching entity via AsyncDatabase."""
    from nextorm.async_database import AsyncDatabase

    class _AgetUser(Entity):
        _table_name_ = "_aget_user"
        name: Req[str]

    db = AsyncDatabase(entities=[_AgetUser])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)

    from nextorm.session import db_session as _sess

    async with _sess:
        await db.asave(_AgetUser(name="frank"))

    async with _sess:
        result = await _AgetUser.aget(name="frank")
        assert result is not None
        assert result.name == "frank"

    async with _sess:
        none_result = await _AgetUser.aget(name="nobody")
        assert none_result is None

    await db.close()
