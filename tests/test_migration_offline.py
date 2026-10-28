"""Offline migration checks: no database server required.

Every file in ``migrations/versions`` is executed, in ``down_revision`` order,
against a recording Alembic ``Operations`` object in offline (SQL-script) mode.
The resulting schema is then diffed against ``Base.metadata``.

This is what stops the hand-written migrations from drifting out of sync with
the models - including the naming-convention-mangled constraint names that are
easy to get wrong by hand - and it works without a live PostgreSQL. The live
``alembic check`` equivalent lives in ``test_migrations.py``.
"""
from __future__ import annotations

import io
import pathlib
import re

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.core.database import Base
from app.models import (  # noqa: F401
    Address,
    Banner,
    CartItem,
    Category,
    Order,
    OrderItem,
    OrderStatusHistory,
    Product,
    RefreshToken,
    User,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "migrations/versions"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    if not ok:
        print(f"FAIL  {name}" + (f"\n      {detail}" if detail else ""))


# --------------------------------------------------------------------- loading
class RecordingOperations(Operations):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.tables: dict[str, sa.Table] = {}
        self.indexes: list[tuple] = []

    def create_table(self, table_name, *columns, **kw):  # type: ignore[override]
        table = super().create_table(table_name, *columns, **kw)
        self.tables[table.name] = table
        return table

    def create_index(self, name, table_name, columns, **kw):  # type: ignore[override]
        self.indexes.append((name, table_name, list(columns), kw))
        return super().create_index(name, table_name, columns, **kw)

    def add_column(self, table_name, column, **kw):  # type: ignore[override]
        # ``add_column`` mutates an existing table rather than creating one, so
        # the recording table has to be updated explicitly for the offline diff
        # to see a column added by a later migration (e.g. orders.payment_status).
        table = self.tables.get(table_name)
        if table is not None:
            column = column._copy()
            table.append_column(column)
        return super().add_column(table_name, column, **kw)


def _module(path: pathlib.Path) -> dict:
    """Import a migration file and return its namespace."""
    src = path.read_text(encoding="utf-8")
    ns = {"sa": sa, "sqlalchemy": sa, "__name__": f"migrations.versions.{path.stem}"}
    exec(compile(src, str(path), "exec"), ns)  # noqa: S102
    return ns


def discover_chain() -> list[tuple[str, pathlib.Path, dict]]:
    """Return ``(revision, path, namespace)`` ordered base-first."""
    modules = []
    for path in sorted(VERSIONS.glob("*.py")):
        if path.name.startswith("__"):
            continue
        ns = _module(path)
        modules.append((ns["revision"], path, ns))
    if not modules:
        return []

    by_revision = {rev: (path, ns) for rev, path, ns in modules}
    children: dict[str | None, list[str]] = {}
    for rev, _path, ns in modules:
        children.setdefault(ns["down_revision"], []).append(rev)

    roots = children.get(None, [])
    assert len(roots) == 1, f"expected exactly one base revision, found {roots}"

    chain: list[tuple[str, pathlib.Path, dict]] = []
    current: str | None = roots[0]
    while current is not None:
        path, ns = by_revision[current]
        chain.append((current, path, ns))
        following = children.get(current, [])
        assert len(following) <= 1, f"revision {current} has branches: {following}"
        current = following[0] if following else None

    assert len(chain) == len(modules), (
        f"orphaned revisions (not reachable from the base): "
        f"{sorted(set(by_revision) - {r for r, _, _ in chain})}"
    )
    return chain


def _recorder() -> tuple[RecordingOperations, io.StringIO]:
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    recorder = RecordingOperations(context)
    # Alembic's proxy-based get_bind() is unreachable from a subclass, but in
    # offline mode impl.bind is a MockConnection that renders DDL to the buffer.
    recorder.get_bind = lambda: context.impl.bind
    return recorder, buffer


def run_upgrade() -> tuple[RecordingOperations, str, list[str]]:
    recorder, buffer = _recorder()
    chain = discover_chain()
    for rev, path, ns in chain:
        # The module does `from alembic import op`; rebind it in the globals the
        # function closes over.
        ns["op"] = recorder
        ns["upgrade"]()
    return recorder, buffer.getvalue(), [rev for rev, _, _ in chain]


def run_downgrade() -> str:
    recorder, buffer = _recorder()
    chain = discover_chain()
    for _rev, _path, ns in reversed(chain):
        ns["op"] = recorder
        ns["downgrade"]()
    return buffer.getvalue()


def _norm(t) -> str:
    return str(sa.types.to_instance(t)).replace(" ", "")


@pytest.fixture(scope="module")
def upgrade_result():
    return run_upgrade()


# ----------------------------------------------------------------------- tests
def test_chain_is_linear_and_starts_from_0001():
    chain = discover_chain()
    revisions = [rev for rev, _, _ in chain]
    assert revisions[0] == "0001_init"
    assert len(revisions) == len(set(revisions)), "duplicate revision ids"


def test_migration_creates_exactly_the_modelled_tables(upgrade_result):
    rec, _sql, _chain = upgrade_result
    modelled = set(Base.metadata.tables) - {"alembic_version"}
    check("table set matches", set(rec.tables) == modelled,
          f"migration={sorted(rec.tables)} models={sorted(modelled)}")


def test_columns_types_nullability_and_defaults_match(upgrade_result):
    rec, _sql, _chain = upgrade_result
    for name in sorted(set(rec.tables)):
        model_t, mig_t = Base.metadata.tables[name], rec.tables[name]
        mcols = {c.name: c for c in model_t.columns}
        xcols = {c.name: c for c in mig_t.columns}
        check(f"{name} column set", set(mcols) == set(xcols),
              f"only-in-models={sorted(set(mcols) - set(xcols))} "
              f"only-in-migration={sorted(set(xcols) - set(mcols))}")
        for cname in sorted(set(mcols) & set(xcols)):
            mc, xc = mcols[cname], xcols[cname]
            check(f"{name}.{cname} type", _norm(mc.type) == _norm(xc.type),
                  f"{_norm(mc.type)} != {_norm(xc.type)}")
            check(f"{name}.{cname} nullable", bool(mc.nullable) == bool(xc.nullable),
                  f"{mc.nullable} != {xc.nullable}")
            m_def = _norm(mc.server_default.arg) if mc.server_default is not None else None
            x_def = _norm(xc.server_default.arg) if xc.server_default is not None else None
            check(f"{name}.{cname} server_default", m_def == x_def, f"{m_def!r} != {x_def!r}")


def test_constraint_names_match_the_naming_convention(upgrade_result):
    rec, _sql, _chain = upgrade_result
    for name in sorted(set(rec.tables)):
        model_t, mig_t = Base.metadata.tables[name], rec.tables[name]

        def names(table, kind):
            return sorted(c.name for c in table.constraints
                          if type(c).__name__ == kind and c.name is not None)

        for kind, label in (
            ("CheckConstraint", "check"),
            ("PrimaryKeyConstraint", "pk"),
            ("ForeignKeyConstraint", "fk"),
            ("UniqueConstraint", "uq"),
        ):
            check(f"{name} {label} names", names(model_t, kind) == names(mig_t, kind),
                  f"models={names(model_t, kind)} migration={names(mig_t, kind)}")

        m_texts = sorted(str(c.sqltext) for c in model_t.constraints
                         if type(c).__name__ == "CheckConstraint")
        x_texts = sorted(str(c.sqltext) for c in mig_t.constraints
                         if type(c).__name__ == "CheckConstraint")
        check(f"{name} check text", m_texts == x_texts, f"{m_texts} != {x_texts}")


def test_indexes_match(upgrade_result):
    rec, _sql, _chain = upgrade_result

    def using(value):
        # SQLAlchemy normalises an absent "using" to False on the model side and
        # leaves it as None in the migration kwargs; both mean the default.
        return value or None

    for name in sorted(set(rec.tables)):
        model_idx = sorted(
            (i.name, tuple(c.name for c in i.columns), bool(i.unique),
             using(i.dialect_options["postgresql"].get("using")))
            for i in Base.metadata.tables[name].indexes
        )
        mig_idx = sorted(
            (n, tuple(cols), bool(kw.get("unique", False)), using(kw.get("postgresql_using")))
            for n, tbl, cols, kw in rec.indexes
            if tbl == name
        )
        check(f"{name} indexes", model_idx == mig_idx, f"models={model_idx} migration={mig_idx}")


def test_address_default_index_is_partial(upgrade_result):
    rec, _sql, _chain = upgrade_result
    partial = [i for i in rec.indexes
               if i[1] == "addresses" and i[3].get("postgresql_where") is not None]
    check("partial unique index exists on addresses", len(partial) == 1)
    if partial:
        _name, _table, cols, kw = partial[0]
        check("partial index is unique", bool(kw.get("unique")))
        check("partial index is scoped to user_id", cols == ["user_id"])
        check("partial index filters on is_default", "is_default" in str(kw["postgresql_where"]))


def test_product_tags_use_a_gin_index(upgrade_result):
    rec, _sql, _chain = upgrade_result
    gin = [i for i in rec.indexes if i[0] == "ix_products_tags"]
    check("products tags index exists", len(gin) == 1)
    if gin:
        check("tags index uses GIN", gin[0][3].get("postgresql_using") == "gin",
              str(gin[0][3]))


def test_cascade_and_restrict_are_declared(upgrade_result):
    """Every foreign key declares the delete rule the design calls for.

    Keyed by ``(table, column)`` rather than by table, because ``cart_items`` has
    two foreign keys and they do not share a rule.
    """
    rec, _sql, _chain = upgrade_result
    expected = {
        ("addresses", "user_id"): "CASCADE",
        ("refresh_tokens", "user_id"): "CASCADE",
        # A category that still has products is a 409, never a silent cascade.
        ("products", "category_id"): "RESTRICT",
        # Deleting a user or a product takes their cart lines with them, so a
        # line can never outlive the row it points at.
        ("cart_items", "user_id"): "CASCADE",
        ("cart_items", "product_id"): "CASCADE",
        # An order is a financial record: deleting the account that placed it
        # takes the order with it, but a product row cannot be hard-deleted out
        # from under money that has already been charged.
        ("orders", "user_id"): "CASCADE",
        ("order_items", "order_id"): "CASCADE",
        ("order_items", "product_id"): "RESTRICT",
        ("order_status_history", "order_id"): "CASCADE",
        # The audit trail outlives the user who caused the change.
        ("order_status_history", "changed_by_id"): "SET NULL",
        # A payment cannot outlive the order it paid for.
        ("payments", "order_id"): "CASCADE",
    }
    found: dict[tuple[str, str], str] = {}
    for table, mig_t in rec.tables.items():
        for fk in mig_t.constraints:
            if type(fk).__name__ == "ForeignKeyConstraint":
                for element in fk.elements:
                    found[(table, element.parent.name)] = fk.ondelete

    check("all foreign keys accounted for", set(found) == set(expected),
          f"unexpected={sorted(set(found) - set(expected))} "
          f"missing={sorted(set(expected) - set(found))}")
    for key, rule in expected.items():
        check(f"{key[0]}.{key[1]} delete rule is {rule}", found.get(key) == rule,
              f"got {found.get(key)!r}")


def test_rendered_upgrade_sql_is_valid_postgres(upgrade_result):
    _rec, sql, chain = upgrade_result
    for fragment in (
        "CREATE TYPE user_role AS ENUM",
        "CREATE TYPE order_status AS ENUM",
        "CREATE TYPE payment_method AS ENUM",
        "CREATE TYPE payment_status AS ENUM",
        "CREATE TABLE users",
        "CREATE TABLE addresses",
        "CREATE TABLE refresh_tokens",
        "CREATE TABLE categories",
        "CREATE TABLE products",
        "CREATE TABLE banners",
        "CREATE TABLE cart_items",
        "CREATE TABLE orders",
        "CREATE TABLE order_items",
        "CREATE TABLE order_status_history",
        "CREATE TABLE payments",
        "ON DELETE CASCADE",
        "ON DELETE RESTRICT",
        "ON DELETE SET NULL",
        "USING gin",
    ):
        check(f"DDL contains {fragment!r}", fragment in sql)
    check("no unresolved placeholders", "%(" not in sql, sql)


def test_order_status_enum_is_closed(upgrade_result):
    """The five frontend-expected values, and nothing else.

    The Angular client indexes a total ``Record`` of badge classes by status, so
    a value outside this set renders ``undefined`` as a CSS class. That makes
    the enum closed in the database rather than merely validated at the edge.
    """
    _rec, sql, _chain = upgrade_result
    match = re.search(
        r"CREATE TYPE order_status AS ENUM \((.*?)\);", sql, re.DOTALL | re.IGNORECASE
    )
    check("order_status is a native enum", match is not None, sql[:400])
    if match:
        values = re.findall(r"'([^']+)'", match.group(1))
        check(
            "order_status has exactly the five expected values",
            values == ["pending", "confirmed", "shipped", "delivered", "cancelled"],
            str(values),
        )
    payment = re.search(
        r"CREATE TYPE payment_method AS ENUM \((.*?)\);", sql, re.DOTALL | re.IGNORECASE
    )
    check("payment_method is a native enum", payment is not None)
    if payment:
        values = re.findall(r"'([^']+)'", payment.group(1))
        check("payment_method is cod or online", values == ["cod", "online"], str(values))


def test_money_invariants_are_database_enforced(upgrade_result):
    """The totals cannot be persisted inconsistent with their own parts."""
    rec, _sql, _chain = upgrade_result
    orders = {
        c.name: str(c.sqltext) for c in rec.tables["orders"].constraints
        if type(c).__name__ == "CheckConstraint"
    }
    check(
        "total must equal subtotal + delivery - discount",
        "total = subtotal + delivery_charge - discount" in orders.values(),
        str(orders),
    )
    items = {
        c.name: str(c.sqltext) for c in rec.tables["order_items"].constraints
        if type(c).__name__ == "CheckConstraint"
    }
    check(
        "a line total must equal price x quantity",
        "line_total = unit_price * quantity" in items.values(),
        str(items),
    )


def test_downgrade_drops_everything_in_reverse_order():
    sql = run_downgrade()
    at = {t: sql.find(f"DROP TABLE {t}") for t in (
        "refresh_tokens", "addresses", "products", "cart_items",
        "order_status_history", "order_items", "orders", "banners",
        "payments", "categories", "users",
    )}
    check("all tables dropped", all(p >= 0 for p in at.values()), sql)
    # Revisions are downgraded newest-first, so 0003-0006's tables drop before
    # 0002's. What matters is that a table never drops before something it
    # references, and that the enum types go last of all.
    check("payments drops first", at["payments"] == min(at.values()))
    check("payments drops before orders", at["payments"] < at["orders"])
    check("cart_items drops before products", at["cart_items"] < at["products"])
    check("order_items drops before orders", at["order_items"] < at["orders"])
    check("order_status_history drops before orders",
          at["order_status_history"] < at["orders"])
    check("order_items drops before products", at["order_items"] < at["products"])
    check("orders drop before users", at["orders"] < at["users"])
    check("products drop before categories", at["products"] < at["categories"])
    for child in ("refresh_tokens", "addresses", "products", "cart_items",
                  "order_status_history", "order_items", "orders", "categories"):
        check(f"{child} drops before users", at[child] < at["users"])
    # An enum must be dropped after every table whose columns use it. It does
    # *not* have to outlive unrelated tables: the chain downgrades newest-first,
    # so 0004's enums go before 0001's `users` table.
    enum_users = {
        "user_role": ["users"],
        "order_status": ["orders", "order_status_history"],
        "payment_method": ["orders"],
        # payment_status is introduced with its own column, so 0006 drops the
        # column and then the type before `orders` itself is dropped by 0004.
        "payment_status": ["payments"],
    }
    for enum_name, tables in enum_users.items():
        check(f"{enum_name} is dropped", f"DROP TYPE {enum_name}" in sql)
        drop_type_at = sql.find(f"DROP TYPE {enum_name}")
        for table in tables:
            drop_table_at = sql.find(f"DROP TABLE {table}")
            check(f"{table} exists to be dropped", drop_table_at >= 0)
            check(
                f"{enum_name} drops after {table}",
                drop_type_at > drop_table_at,
                f"DROP TYPE at {drop_type_at}, DROP TABLE at {drop_table_at}",
            )
    drop_payment_status_at = sql.find("DROP TYPE payment_status")
    drop_column_at = sql.find("ALTER TABLE orders DROP COLUMN payment_status")
    check("orders.payment_status column is dropped", drop_column_at >= 0)
    check(
        "payment_status type drops after its column",
        drop_payment_status_at > drop_column_at,
        f"DROP TYPE at {drop_payment_status_at}, DROP COLUMN at {drop_column_at}",
    )


def test_no_summaries():
    assert not FAIL, f"{len(FAIL)} drift(s): {FAIL}"
    print(f"\n{len(PASS)} offline migration assertions passed")
