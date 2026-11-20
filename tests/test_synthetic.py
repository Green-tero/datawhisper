"""Tests for Synthetic Mock Data Generator."""

from __future__ import annotations

import pytest
from datawhisper.core.models import CatalogSnapshot, ColumnInfo, ForeignKeyInfo, TableInfo
from datawhisper.core.synthetic import SyntheticConfig, SyntheticDataGenerator


@pytest.fixture
def sample_catalog() -> CatalogSnapshot:
    customers = TableInfo(
        name="customers",
        columns=[
            ColumnInfo(name="id", type="INTEGER", primary_key=True),
            ColumnInfo(name="full_name", type="TEXT"),
            ColumnInfo(name="email", type="TEXT"),
        ],
    )
    orders = TableInfo(
        name="orders",
        columns=[
            ColumnInfo(name="id", type="INTEGER", primary_key=True),
            ColumnInfo(name="customer_id", type="INTEGER"),
            ColumnInfo(name="amount", type="REAL"),
            ColumnInfo(name="status", type="TEXT"),
        ],
        foreign_keys=[
            ForeignKeyInfo(
                from_table="orders",
                from_columns=["customer_id"],
                to_table="customers",
                to_columns=["id"],
            )
        ],
    )
    return CatalogSnapshot(dialect="sqlite", tables=[customers, orders])


def test_synthetic_data_generation_preserves_fk_integrity(
    sample_catalog: CatalogSnapshot,
) -> None:
    gen = SyntheticDataGenerator(SyntheticConfig(rows_per_table=15, seed=123))
    dataset = gen.generate_dataset(sample_catalog)

    assert "customers" in dataset
    assert "orders" in dataset
    assert len(dataset["customers"]) == 15
    assert len(dataset["orders"]) == 15

    # Check that customer IDs are unique integers 1..15
    customer_ids = {row[0] for row in dataset["customers"]}
    assert len(customer_ids) == 15

    # Check referential integrity: every order's customer_id must exist in customer_ids
    for order in dataset["orders"]:
        order_cust_id = order[1]
        assert order_cust_id in customer_ids


def test_synthetic_in_memory_engine_execution(sample_catalog: CatalogSnapshot) -> None:
    gen = SyntheticDataGenerator(SyntheticConfig(rows_per_table=10))
    with gen.generate_in_memory_engine(sample_catalog) as engine:
        # Verify queries execute cleanly against synthetic tables
        res_cust = engine.execute("SELECT COUNT(*) FROM customers")
        assert res_cust.rows[0][0] == 10

        res_join = engine.execute(
            "SELECT c.full_name, COUNT(o.id) "
            "FROM customers c "
            "LEFT JOIN orders o ON c.id = o.customer_id "
            "GROUP BY c.id"
        )
        assert len(res_join.rows) == 10
