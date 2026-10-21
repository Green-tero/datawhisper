"""Shared fixtures: an on-disk SQLite store with null-heavy, mixed-type data."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from datawhisper.core.config import DataWhisperConfig
from datawhisper.core.database import ExecutionEngine


@pytest.fixture
def sqlite_db_path(tmp_path: Path) -> Path:
    """Build a small e-commerce schema: 5 customers, 12 orders, deliberate NULLs."""
    path = tmp_path / "shop.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT,
            country TEXT,
            signup_date TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER,
            amount REAL,
            status TEXT,
            placed_at TEXT
        );
        INSERT INTO customers VALUES
            (1, 'Ada Lovelace', 'UK', '2024-01-11'),
            (2, 'Alan Turing', 'UK', '2024-02-02'),
            (3, 'Maryam Mirzakhani', 'IR', '2024-03-15'),
            (4, 'Grace Hopper', 'US', '2024-04-01'),
            (5, 'N/A', NULL, NULL);
        INSERT INTO orders VALUES
            (1, 1, 120.5,  'paid',     '2024-05-01'),
            (2, 1, 35.0,   'paid',     '2024-05-03'),
            (3, 2, 89.9,   'pending',  '2024-05-04'),
            (4, 2, NULL,   'refunded', '2024-05-05'),
            (5, 3, 250.0,  'paid',     '2024-05-06'),
            (6, 3, 19.99,  'pending',  '2024-05-07'),
            (7, 4, 64.0,   'paid',     '2024-05-08'),
            (8, 4, 64.0,   'pending',  '2024-05-09'),
            (9, 5, NULL,   'pending',  '2024-05-10'),
            (10, 5, 320.0, 'paid',     '2024-05-11'),
            (11, 1, 15.0,  'refunded', '2024-05-12'),
            (12, 3, 75.25, 'paid',     '2024-05-13');
        CREATE TABLE empty_metrics (
            id INTEGER PRIMARY KEY,
            gauge REAL
        );
        """
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def engine(sqlite_db_path: Path) -> Iterator[ExecutionEngine]:
    """Read-only engine over the fixture database; always closed afterwards."""
    instance = ExecutionEngine.from_source(sqlite_db_path, DataWhisperConfig.from_env({}).safety)
    yield instance
    instance.close()
