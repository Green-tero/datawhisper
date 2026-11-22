"""Dashboard helpers: correlation alignment and HTML escaping."""

from __future__ import annotations

import pytest
from datawhisper.core.models import PlanInfo, PlanNode
from datawhisper.integrations.github_bot import BenchmarkResult, DriftReport, TableDrift
from datawhisper.visualization.dashboard import (
    _aligned_numeric,
    _pearson,
    auto_charts,
    plan_html,
    render_audit_html,
)


def test_correlation_aligns_rows_containing_nulls() -> None:
    columns = ["a", "b"]
    rows = [[1, 10], [2, None], [3, 30], [None, 40], [5, 50]]
    xs, ys = _aligned_numeric(columns, rows, "a", "b")
    assert xs == [1.0, 3.0, 5.0]
    assert ys == [10.0, 30.0, 50.0]
    assert _pearson(xs, ys) == pytest.approx(1.0)


def test_plan_html_escapes_untrusted_text() -> None:
    html = plan_html(
        PlanInfo(
            dialect="duckdb",
            nodes=[
                PlanNode(
                    node_type="SEQ_SCAN",
                    table="<img src=x onerror=alert(1)>",
                    detail="<script>alert(1)</script>",
                )
            ],
        )
    )
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html


def test_auto_charts_generates_blocks() -> None:
    columns = ["date", "category", "amount"]
    rows = [
        ["2025-01-01", "Furniture", 100.0],
        ["2025-01-02", "Electronics", 200.0],
        ["2025-01-03", "Furniture", 150.0],
    ]
    charts = auto_charts(columns, rows)
    assert len(charts) > 0
    assert any("amount" in c.heading.lower() for c in charts)


def test_render_audit_html_structure() -> None:
    drift = DriftReport(
        base_ref="main",
        head_ref="feature",
        tables=[TableDrift(table="users", status="modified", added_columns=["email"])],
    )
    benchmarks = [
        BenchmarkResult(
            label="lookup",
            sql="SELECT 1",
            baseline_ms=10.0,
            candidate_ms=11.0,
            regression=False,
        )
    ]
    html = render_audit_html(
        drift=drift,
        benchmarks=benchmarks,
        title="CI Audit",
        subtitle="PR 42",
    )
    assert "<!DOCTYPE html>" in html
    assert "CI Audit" in html
    assert "PR 42" in html
    assert "Schema drift" in html
