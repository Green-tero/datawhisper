"""Tests for Automated Index Advisor & Cost Optimizer."""

from __future__ import annotations

from datawhisper.core.advisor import IndexAdvisor
from datawhisper.core.database import ExecutionEngine


def test_index_advisor_suggests_where_and_order_indexes() -> None:
    advisor = IndexAdvisor(dialect="sqlite")
    sql = "SELECT id, amount FROM orders WHERE status = 'COMPLETED' ORDER BY placed_at DESC"
    report = advisor.advise(sql)

    assert report.has_recommendations
    assert len(report.recommendations) >= 1

    rec = report.recommendations[0]
    assert rec.table == "orders"
    assert "status" in rec.columns
    assert "CREATE INDEX" in rec.sql
    assert rec.estimated_cost_reduction_pct >= 70.0


def test_index_advisor_handles_joins_and_composite_predicates() -> None:
    advisor = IndexAdvisor(dialect="postgres")
    sql = (
        "SELECT c.full_name, o.amount "
        "FROM customers c "
        "JOIN orders o ON c.id = o.customer_id "
        "WHERE o.status = 'paid' AND o.amount > 100.0"
    )
    report = advisor.advise(sql)
    assert report.has_recommendations

    # Postgres dialect uses CONCURRENTLY
    pg_sqls = [r.sql for r in report.recommendations]
    assert any("CONCURRENTLY" in s for s in pg_sqls)


def test_execution_engine_advise_query(engine: ExecutionEngine) -> None:
    sql = "SELECT * FROM customers WHERE email = 'ada@example.com'"
    report = engine.advise_query(sql)

    assert report.has_recommendations
    assert any("email" in r.columns for r in report.recommendations)
