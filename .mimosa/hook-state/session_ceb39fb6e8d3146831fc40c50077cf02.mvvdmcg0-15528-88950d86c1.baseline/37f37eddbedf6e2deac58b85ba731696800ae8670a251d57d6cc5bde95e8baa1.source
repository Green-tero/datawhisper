"""Dashboard helpers: correlation alignment and HTML escaping."""

from __future__ import annotations

import pytest
from datawhisper.core.models import PlanInfo, PlanNode
from datawhisper.visualization.dashboard import _aligned_numeric, _pearson, plan_html


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
