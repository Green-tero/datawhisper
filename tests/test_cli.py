"""Tests for the Typer-based CLI commands."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from datawhisper.cli import app
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def sqlite_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "test_cli.sqlite")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT);
        INSERT INTO customers VALUES (1, 'Ada', 'ada@example.com'), (2, 'Alan', 'alan@example.com');
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, status TEXT);
        INSERT INTO orders VALUES (1, 1, 150.0, 'paid'), (2, 2, 75.0, 'pending');
        """
    )
    conn.commit()
    conn.close()
    return db_path


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "datawhisper" in result.stdout


def test_cli_explain_with_source_and_db(sqlite_db: str) -> None:
    # Test --source flag
    res1 = runner.invoke(app, ["explain", "SELECT * FROM customers", "--source", sqlite_db])
    assert res1.exit_code == 0
    assert "Query plan" in res1.stdout or "cost gate" in res1.stdout

    # Test backward-compatible --db flag
    res2 = runner.invoke(app, ["explain", "SELECT * FROM customers", "--db", sqlite_db])
    assert res2.exit_code == 0


def test_cli_inspect_with_source_and_db(sqlite_db: str) -> None:
    res1 = runner.invoke(app, ["inspect", "--source", sqlite_db])
    assert res1.exit_code == 0
    assert "customers" in res1.stdout

    res2 = runner.invoke(app, ["inspect", "--db", sqlite_db])
    assert res2.exit_code == 0
    assert "customers" in res2.stdout


def test_cli_advise(sqlite_db: str) -> None:
    sql = "SELECT * FROM orders WHERE status = 'paid' ORDER BY amount DESC"
    result = runner.invoke(app, ["advise", sql, "--source", sqlite_db])
    assert result.exit_code == 0
    assert "Recommended Indexes" in result.stdout or "Index Advisor" in result.stdout
    assert "orders" in result.stdout


def test_cli_contract_success(sqlite_db: str) -> None:
    result = runner.invoke(
        app,
        [
            "contract",
            "--source",
            sqlite_db,
            "--table",
            "customers",
            "--not-null",
            "name",
            "--min-rows",
            "2",
        ],
    )
    assert result.exit_code == 0
    assert "Data contract passed" in result.stdout


def test_cli_contract_failure(sqlite_db: str) -> None:
    # Fails because table has only 2 rows, min-rows is 50
    result = runner.invoke(
        app,
        ["contract", "--source", sqlite_db, "--table", "customers", "--min-rows", "50"],
    )
    assert result.exit_code == 1
    assert "Data contract failed" in result.stdout


def test_cli_ask_explain_only(sqlite_db: str) -> None:
    result = runner.invoke(
        app,
        ["ask", "what is total revenue", "--source", sqlite_db, "--explain-only"],
    )
    assert result.exit_code == 0
    assert "pruned schema context" in result.stdout


def test_cli_audit_dry_run(tmp_path: Path) -> None:
    base_sql = tmp_path / "base.sql"
    head_sql = tmp_path / "head.sql"
    base_sql.write_text("CREATE TABLE users (id INTEGER PRIMARY KEY);")
    head_sql.write_text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);")

    result = runner.invoke(
        app,
        [
            "audit",
            "--base-sql",
            str(base_sql),
            "--head-sql",
            str(head_sql),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "Schema Audit" in result.stdout or "audit" in result.stdout.lower()
