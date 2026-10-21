"""Sandbox penetration tests: every classic SQL injection / escape vector."""

from __future__ import annotations

import pytest
from datawhisper.core.security import (
    SQLSecurityError,
    ValidationViolation,
    validate_or_raise,
    validate_sql,
)

# (sql, dialect, should_pass)
ATTACK_CASES = [
    # Mutation statements
    ("DELETE FROM orders", "sqlite", False),
    ("DROP TABLE orders", "sqlite", False),
    ("UPDATE orders SET amount = 0", "sqlite", False),
    ("INSERT INTO orders VALUES (1, 1, 1.0, 'x', 'y')", "sqlite", False),
    ("CREATE TABLE pwn (x INTEGER)", "sqlite", False),
    ("ALTER TABLE orders ADD COLUMN z INTEGER", "sqlite", False),
    # System / environment escape hatches
    ("PRAGMA table_info(orders)", "sqlite", False),
    ("PRAGMA data_version", "sqlite", False),
    ("ATTACH DATABASE 'evil.db' AS evil", "sqlite", False),
    ("VACUUM", "sqlite", False),
    ("INSTALL httpfs", "duckdb", False),
    ("COPY orders TO 'exfil.csv'", "duckdb", False),
    # Multi-statement and comment bypasses
    ("SELECT 1; DROP TABLE orders", "sqlite", False),
    ("SELECT 1;SELECT 2", "sqlite", False),
    ("SELECT * FROM orders; -- trailing comment", "sqlite", False),
    ("SELECT 1 --; DROP TABLE orders", "sqlite", False),
    ("SELECT /* inline */ * FROM orders", "sqlite", False),
    # Dangerous function exploitation
    ("SELECT * FROM read_csv('secret.csv')", "duckdb", False),
    ("SELECT * FROM read_parquet('/etc/passwd')", "duckdb", False),
    ("SELECT load_extension('evil')", "sqlite", False),
    ("SELECT getenv('GITHUB_TOKEN')", "duckdb", False),
    # Catalog/schema escape after a hypothetical ATTACH
    ("SELECT * FROM secret_db.main.orders", "duckdb", False),
    ("SELECT * FROM attached.orders", "sqlite", False),
    # Blocked statements nested inside a WITH (root inspection catches them)
    ("WITH t AS (SELECT 1 AS x) DELETE FROM orders", "sqlite", False),
]

VALID_CASES = [
    ("SELECT 1", "sqlite"),
    ("SELECT COUNT(*) FROM orders", "sqlite"),
    ("WITH t AS (SELECT 1 AS x) SELECT * FROM t", "sqlite"),
    ("SELECT status FROM orders UNION SELECT 'x' AS status", "sqlite"),
    ("SELECT (SELECT MAX(amount) FROM orders) AS m", "sqlite"),
    ("SELECT o.status, SUM(o.amount) FROM orders o GROUP BY o.status", "sqlite"),
    ("SELECT * FROM main.orders", "sqlite"),
    ("SELECT * FROM information_schema.tables", "duckdb"),
    ("SELECT list_transform([1, 2], x -> x + 1)", "duckdb"),
    # One trailing semicolon is tolerated (copy-paste ergonomics)
    ("SELECT 1;", "sqlite"),
]


@pytest.mark.parametrize(("sql", "dialect", "should_pass"), ATTACK_CASES)
def test_attack_vectors(sql: str, dialect: str, should_pass: bool) -> None:
    result = validate_sql(sql, dialect)  # type: ignore[arg-type]
    assert result.ok is should_pass, f"violations: {[v.code for v in result.violations]}"


@pytest.mark.parametrize(("sql", "dialect"), VALID_CASES)
def test_legitimate_queries_pass(sql: str, dialect: str) -> None:
    result = validate_sql(sql, dialect)  # type: ignore[arg-type]
    assert result.ok, f"unexpectedly blocked: {[v.code for v in result.violations]}"


def test_rejections_are_structured_and_explained() -> None:
    result = validate_sql("SELECT * FROM read_csv('x.csv') UNION ALL SELECT 1", "duckdb")
    assert not result.ok
    codes = [v.code for v in result.violations]
    assert "BLOCKED_FUNCTION" in codes
    message = " ".join(v.message for v in result.violations)
    assert "read_csv" in message


def test_unparseable_sql_fails_closed() -> None:
    result = validate_sql("SELECT FROM WHERE", "sqlite")
    assert not result.ok
    assert result.violations[0].code in {"PARSE_ERROR", "MULTI_STATEMENT"}


def test_empty_statement_rejected() -> None:
    result = validate_sql("   ", "sqlite")
    assert not result.ok
    assert result.violations[0].code == "EMPTY"


def test_validate_or_raise_carries_violations() -> None:
    with pytest.raises(SQLSecurityError) as excinfo:
        validate_or_raise("DROP TABLE orders", "sqlite")
    assert excinfo.value.violations
    assert all(isinstance(v, ValidationViolation) for v in excinfo.value.violations)


def test_normalized_sql_is_semicolon_free() -> None:
    result = validate_or_raise("SELECT COUNT(*) FROM orders;", "sqlite")
    assert result.sql == "SELECT COUNT(*) FROM orders"
