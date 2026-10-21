"""Fixtures: a PII-bearing SQLite store, columnar files and a 60-table graph."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from datawhisper.core.config import DataWhisperConfig
from datawhisper.core.database import ExecutionEngine
from datawhisper.core.models import CatalogSnapshot, ColumnInfo, ForeignKeyInfo, TableInfo


@pytest.fixture
def sqlite_db_path(tmp_path: Path) -> Path:
    """Customers carry every supported PII class; orders have NULL amounts."""
    path = tmp_path / "shop.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            full_name TEXT,
            email TEXT,
            phone TEXT,
            card_number TEXT,
            ssn TEXT,
            api_token TEXT,
            country TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            amount REAL,
            status TEXT,
            placed_at TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            kind TEXT,
            payload TEXT
        );
        INSERT INTO customers VALUES
            (1, 'Ada Lovelace', 'ada@example.com', '+1 555-123-4567',
             '4111 1111 1111 1111', '123-45-6789',
             'sk-live-abcdefghijklmnopqrstuvwx', 'UK'),
            (2, 'Alan Turing', 'alan@example.com', '+1 555-987-6543',
             '5500 0000 0000 0004', '234-56-7890',
             'ghp_abcdefghijklmnopqrstuvwxyz0123456789', 'UK'),
            (3, 'Maryam Mirzakhani', 'maryam@example.org', '+98 912 345 6789',
             '4012 8888 8888 1881', '345-67-8901',
             'AKIAIOSFODNN7EXAMPLE', 'IR'),
            (4, 'Grace Hopper', NULL, NULL, NULL, NULL, NULL, 'US');
        INSERT INTO orders VALUES
            (1, 1, 120.5,  'paid',     '2024-05-01'),
            (2, 1, 35.0,   'paid',     '2024-05-03'),
            (3, 2, 89.9,   'pending',  '2024-05-04'),
            (4, 2, NULL,   'refunded', '2024-05-05'),
            (5, 3, 250.0,  'paid',     '2024-05-06'),
            (6, 3, 19.99,  'pending',  '2024-05-07'),
            (7, 4, 64.0,   'paid',     '2024-05-08'),
            (8, 4, 64.0,   'pending',  '2024-05-09'),
            (9, 4, NULL,   'pending',  '2024-05-10'),
            (10, 4, 320.0, 'paid',     '2024-05-11'),
            (11, 1, 15.0,  'refunded', '2024-05-12'),
            (12, 3, 75.25, 'paid',     '2024-05-13');
        INSERT INTO events VALUES
            (1, 'login', '{"user": "ada@example.com"}'),
            (2, 'click', '{"page": "/pricing"}'),
            (3, 'login', '{"user": "alan@example.com"}'),
            (4, 'error', '{"code": 500}'),
            (5, 'click', '{"page": "/docs"}');
        """
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def config() -> DataWhisperConfig:
    """Default policy with a deterministic PII salt for assertions."""
    return DataWhisperConfig.from_env({"DW_PII_SALT": "test-salt"})


@pytest.fixture
def engine(sqlite_db_path: Path, config: DataWhisperConfig) -> Iterator[ExecutionEngine]:
    instance = ExecutionEngine.from_source(sqlite_db_path, config)
    yield instance
    instance.close()


@pytest.fixture
def parquet_path(tmp_path: Path) -> Path:
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "id": [1, 2, 3, 4],
            "amount": [1.5, 2.5, None, 4.5],
            "status": ["paid", "paid", "pending", "pending"],
            "created_at": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
            "email": ["a@x.com", "b@x.com", "c@x.com", "d@x.com"],
        }
    )
    path = tmp_path / "analytics.parquet"
    pq.write_table(table, path)
    return path


@pytest.fixture
def jsonl_path(tmp_path: Path) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"id": 1, "kind": "login", "user": {"email": "ada@example.com"}}',
                '{"id": 2, "kind": "click", "user": {"email": "alan@example.com"}}',
                '{"id": 3, "kind": "login", "user": {"email": "grace@example.com"}}',
            ]
        ),
        encoding="utf-8",
    )
    return path


def make_large_catalog(table_count: int = 60) -> CatalogSnapshot:
    """Linear FK chain ``t0 <- t1 <- ... <- tN`` used by graph tests."""
    tables: list[TableInfo] = []
    for index in range(table_count):
        columns = [
            ColumnInfo(name="id", type="INTEGER", nullable=False, primary_key=True),
            ColumnInfo(name="label", type="TEXT"),
            ColumnInfo(name="metric_value", type="REAL"),
        ]
        keys: list[ForeignKeyInfo] = []
        if index > 0:
            columns.append(ColumnInfo(name=f"t{index - 1}_id", type="INTEGER"))
            keys.append(
                ForeignKeyInfo(
                    from_table=f"t{index}",
                    from_columns=[f"t{index - 1}_id"],
                    to_table=f"t{index - 1}",
                    to_columns=["id"],
                )
            )
        tables.append(
            TableInfo(name=f"t{index}", columns=columns, row_count=100 + index, foreign_keys=keys)
        )
    return CatalogSnapshot(dialect="sqlite", tables=tables)


@pytest.fixture
def large_catalog() -> CatalogSnapshot:
    return make_large_catalog(60)
