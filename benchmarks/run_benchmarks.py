"""Reproducible performance benchmark suite for DataWhisper core components."""

from __future__ import annotations

import time

from datawhisper.core.config import PrivacyConfig
from datawhisper.core.decision_engine import HeuristicDecisionEngine
from datawhisper.core.models import CatalogSnapshot, ColumnInfo, TableInfo
from datawhisper.core.privacy import PrivacyScrubber
from datawhisper.core.schema_graph import SchemaGraph
from datawhisper.core.security import validate_sql
from datawhisper.core.synthetic import SyntheticConfig, SyntheticDataGenerator


def benchmark_system1_decision(iterations: int = 2000) -> float:
    engine = HeuristicDecisionEngine()
    test_queries = [
        "SELECT * FROM users WHERE id = 1",
        "DROP TABLE users; SELECT 1",
        "SELECT email, count(*) FROM orders GROUP BY email",
        "SELECT * FROM customers WHERE name = 'Alice'",
    ]

    t0 = time.perf_counter()
    for _ in range(iterations):
        for q in test_queries:
            engine.score_risk(q)
    total_time = time.perf_counter() - t0
    avg_us = (total_time / (iterations * len(test_queries))) * 1_000_000
    n_queries = iterations * len(test_queries)
    print(f"[System-1 Decision] {n_queries} queries in {total_time:.3f}s: {avg_us:.2f} µs/query")
    return avg_us


def benchmark_ast_validator(iterations: int = 1000) -> float:
    sql = (
        "SELECT c.id, c.name, sum(o.amount) FROM customers c JOIN orders o "
        "ON c.id = o.customer_id GROUP BY c.id, c.name LIMIT 50"
    )

    t0 = time.perf_counter()
    for _ in range(iterations):
        validate_sql(sql)
    total_time = time.perf_counter() - t0
    avg_us = (total_time / iterations) * 1_000_000
    print(f"[AST Validation]    {iterations} validations in {total_time:.3f}s: {avg_us:.2f} µs/val")
    return avg_us


def benchmark_pii_scrubbing() -> float:
    columns = ["name", "email", "ssn", "notes"]
    rows = [
        [f"User {i}", f"user{i}@example.com", f"000-{i:02d}-1234", f"Normal note {i}"]
        for i in range(2500)
    ]
    # Total cells: 2500 * 4 = 10,000 cells
    scrubber = PrivacyScrubber(config=PrivacyConfig(mode="hash", salt="bench-salt"))

    t0 = time.perf_counter()
    _redacted, report = scrubber.scrub(columns, rows)
    total_time = time.perf_counter() - t0
    ms = total_time * 1000
    print(f"[PII Scrubbing]     10,000 cells in {ms:.2f}ms ({report.total} redacted)")
    return total_time


def benchmark_hybrid_schema_pruning() -> float:
    tables = [
        TableInfo(
            name=f"table_{i}",
            columns=[
                ColumnInfo(name="id", data_type="INTEGER", is_pk=True),
                *[ColumnInfo(name=f"metric_{j}", data_type="REAL") for j in range(5)],
            ],
            row_count=1000,
        )
        for i in range(50)
    ]
    catalog = CatalogSnapshot(dialect="sqlite", tables=tables)
    graph = SchemaGraph(catalog)

    t0 = time.perf_counter()
    for _ in range(50):
        graph.retrieve(query="monthly metric revenue customer table_12 table_34", top_k=5)
    total_time = time.perf_counter() - t0
    avg_ms = (total_time / 50) * 1000
    print(f"[Schema Pruning]    50 tables pruned in {total_time:.3f}s: {avg_ms:.2f} ms/run")
    return avg_ms


def benchmark_synthetic_generator() -> float:
    catalog = CatalogSnapshot(
        dialect="sqlite",
        tables=[
            TableInfo(
                name="parent",
                columns=[
                    ColumnInfo(name="id", data_type="INTEGER", is_pk=True),
                    ColumnInfo(name="name", data_type="TEXT"),
                ],
            ),
            TableInfo(
                name="child",
                columns=[
                    ColumnInfo(name="id", data_type="INTEGER", is_pk=True),
                    ColumnInfo(name="parent_id", data_type="INTEGER", foreign_key="parent.id"),
                    ColumnInfo(name="score", data_type="REAL"),
                ],
            ),
        ],
    )
    generator = SyntheticDataGenerator(config=SyntheticConfig(rows_per_table=500))

    t0 = time.perf_counter()
    generator.generate_dataset(catalog)
    total_time = time.perf_counter() - t0
    print(f"[Synthetic Data]    1,000 referential rows generated in {total_time * 1000:.2f}ms")
    return total_time


if __name__ == "__main__":
    print("=" * 60)
    print("DataWhisper Core Component Benchmarks")
    print("=" * 60)
    benchmark_system1_decision()
    benchmark_ast_validator()
    benchmark_pii_scrubbing()
    benchmark_hybrid_schema_pruning()
    benchmark_synthetic_generator()
    print("=" * 60)
