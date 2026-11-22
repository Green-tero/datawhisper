"""Tests for static SQL migration audit rules."""

from __future__ import annotations

from datawhisper.integrations.migration_audit import analyze_migration_sql


def test_migration_audit_flags_destructive_drop() -> None:
    sql = "ALTER TABLE users DROP COLUMN legacy_field; DROP TABLE archived_orders;"
    report = analyze_migration_sql(sql)
    assert bool(report.destructive)
    assert len(report.destructive) == 2
    messages = [f.message for f in report.destructive]
    assert any("legacy_field" in m for m in messages)
    assert any("archived_orders" in m or "DROP table" in m for m in messages)


def test_migration_audit_flags_non_concurrent_index() -> None:
    sql = "CREATE INDEX idx_users_email ON users (email);"
    report = analyze_migration_sql(sql)
    assert bool(report.locks)
    assert any("without CONCURRENTLY" in f.message for f in report.locks)


def test_migration_audit_passes_concurrent_index() -> None:
    sql = "CREATE INDEX CONCURRENTLY idx_users_email ON users (email);"
    report = analyze_migration_sql(sql)
    assert not bool(report.locks)


def test_migration_audit_missing_fk_index_on_add_column() -> None:
    sql = "ALTER TABLE orders ADD COLUMN user_id INTEGER REFERENCES users(id);"
    report = analyze_migration_sql(sql)
    assert bool(report.missing_fk_indexes)
    assert any("user_id" in f.message for f in report.missing_fk_indexes)


def test_migration_audit_satisfied_fk_index_on_add_column() -> None:
    sql = (
        "ALTER TABLE orders ADD COLUMN user_id INTEGER REFERENCES users(id);\n"
        "CREATE INDEX idx_orders_user ON orders (user_id);"
    )
    report = analyze_migration_sql(sql)
    assert not bool(report.missing_fk_indexes)


def test_migration_audit_fk_index_table_scoping() -> None:
    # Index on users(org_id) must NOT satisfy missing index on orders(org_id)
    sql = (
        "CREATE INDEX idx_users_org ON users (org_id);\n"
        "ALTER TABLE orders ADD COLUMN org_id INTEGER REFERENCES organizations(id);"
    )
    report = analyze_migration_sql(sql)
    assert bool(report.missing_fk_indexes)
    assert any("orders" in f.message and "org_id" in f.message for f in report.missing_fk_indexes)


def test_migration_audit_column_type_rewrite_lock_risk() -> None:
    sql = "ALTER TABLE payments ALTER COLUMN amount TYPE numeric(12, 2);"
    report = analyze_migration_sql(sql)
    assert bool(report.locks)
    assert any("changes a column type" in f.message for f in report.locks)
