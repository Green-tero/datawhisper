"""GitHub bot: drift, PII exposure, plan regressions and sticky comments."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from datawhisper.core.database import ExecutionEngine, SQLiteBackend
from datawhisper.core.models import PlanInfo, PlanNode
from datawhisper.integrations.github_bot import (
    COMMENT_MARKER,
    GitHubAPIError,
    GitHubClient,
    PrFile,
    analyze_pii_exposure,
    compare_plans,
    compute_drift,
    format_audit_report,
    markdown_table,
    merge_drifts,
    parse_benchmark_file,
    run_benchmarks,
    unicode_bar,
)

OLD_DDL = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL,
  age INTEGER,
  legacy_flag BOOLEAN
);
CREATE TABLE audit_events (id INTEGER PRIMARY KEY, kind TEXT);
"""

NEW_DDL = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL,
  age BIGINT,
  is_active BOOLEAN,
  user_ssn TEXT,
  api_key TEXT
);
"""


def _response(status: int, payload: Any = None, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(status_code=status, json=lambda: payload, text=text)


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------
def test_column_level_drift() -> None:
    report = compute_drift(OLD_DDL, NEW_DDL, source="migrations/001.sql")
    users = next(table for table in report.tables if table.table == "users")
    assert {"is_active", "user_ssn", "api_key"}.issubset(set(users.added_columns))
    assert "legacy_flag" in users.removed_columns
    assert any(change.column == "age" for change in users.type_changes)
    assert report.dropped_tables == ["audit_events"]
    assert report.column_changes >= 5


def test_no_drift_for_identical_ddl() -> None:
    assert not compute_drift(OLD_DDL, OLD_DDL).has_changes


def test_unparsable_migration_is_reported() -> None:
    assert compute_drift("CREATE TABLE (broken;", NEW_DDL).parse_errors


def test_merge_drifts_deduplicates() -> None:
    merged = merge_drifts([compute_drift(OLD_DDL, NEW_DDL), compute_drift(OLD_DDL, NEW_DDL)])
    assert len(merged.tables) == 1


# ---------------------------------------------------------------------------
# PII exposure
# ---------------------------------------------------------------------------
def test_pii_exposure_flags_new_sensitive_columns() -> None:
    findings = analyze_pii_exposure(OLD_DDL, NEW_DDL)
    flagged = {(finding.table, finding.column) for finding in findings}
    assert ("users", "user_ssn") in flagged
    assert ("users", "email") not in flagged  # pre-existing column is not new
    categories = {finding.category.value for finding in findings}
    assert "SSN" in categories and "API_KEY" in categories


def test_pii_exposure_covers_new_tables() -> None:
    findings = analyze_pii_exposure("", "CREATE TABLE leads (id INTEGER, contact_email TEXT);")
    assert findings and findings[0].column == "contact_email"
    assert findings[0].reason == "new table"


def test_pii_exposure_ignores_benign_columns() -> None:
    assert analyze_pii_exposure("", "CREATE TABLE metrics (id INTEGER, total_amount REAL);") == []


# ---------------------------------------------------------------------------
# Plan regressions
# ---------------------------------------------------------------------------
def test_index_loss_is_detected() -> None:
    baseline = PlanInfo(dialect="sqlite", nodes=[PlanNode(node_type="INDEX_SCAN", table="orders")])
    candidate = PlanInfo(
        dialect="sqlite",
        nodes=[PlanNode(node_type="SCAN", table="orders", estimated_rows=5000)],
    )
    regressions = compare_plans("orders by status", baseline, candidate)
    assert any(regression.kind == "index_dropped" for regression in regressions)


def test_scan_growth_is_detected() -> None:
    baseline = PlanInfo(
        dialect="duckdb",
        nodes=[PlanNode(node_type="SEQ_SCAN", table="orders", estimated_rows=100)],
    )
    candidate = PlanInfo(
        dialect="duckdb",
        nodes=[PlanNode(node_type="SEQ_SCAN", table="orders", estimated_rows=90_000)],
    )
    regressions = compare_plans("q", baseline, candidate)
    assert any(regression.kind == "scan_growth" for regression in regressions)


def test_cartesian_regression_is_detected() -> None:
    baseline = PlanInfo(dialect="duckdb")
    candidate = PlanInfo(dialect="duckdb", nodes=[PlanNode(node_type="CROSS_PRODUCT")])
    regressions = compare_plans("q", baseline, candidate)
    assert any(regression.kind == "cartesian_product" for regression in regressions)


def test_no_regression_for_equivalent_plans() -> None:
    plan = PlanInfo(dialect="sqlite", nodes=[PlanNode(node_type="INDEX_SCAN", table="t")])
    assert compare_plans("q", plan, plan) == []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_markdown_table_escapes_pipes() -> None:
    table = markdown_table(["a", "b"], [["x|y", "z"]])
    assert "x\\|y" in table


def test_unicode_bar() -> None:
    assert unicode_bar(10, 10, 4) == "████"
    assert unicode_bar(0, 10, 4) == "░░░░"
    assert unicode_bar(5, 10, 4) == "██░░"


def test_report_is_collapsible_and_marked() -> None:
    drift = compute_drift(OLD_DDL, NEW_DDL)
    findings = analyze_pii_exposure(OLD_DDL, NEW_DDL)
    regressions = [
        compare_plans(
            "q",
            PlanInfo(dialect="sqlite", nodes=[PlanNode(node_type="INDEX_SCAN", table="orders")]),
            PlanInfo(dialect="sqlite", nodes=[PlanNode(node_type="SCAN", table="orders")]),
        )[0]
    ]
    report = format_audit_report(
        drift=drift,
        pii=findings,
        plan_regressions=regressions,
        pr_number=7,
        base_ref="main",
        head_ref="feature",
        artifact_url="https://example.com/report",
    )
    assert report.splitlines()[0] == COMMENT_MARKER
    assert report.count("<details>") >= 3
    assert "PR #7" in report
    assert "PII exposure risk" in report
    assert "user_ssn" in report
    assert "index_dropped" in report
    assert "https://example.com/report" in report
    # Findings are column-level only: never a sampled value.
    assert "@" not in report.split("PII exposure risk", 1)[1].split("</details>", 1)[0]


def test_report_marks_clean_audit() -> None:
    report = format_audit_report(drift=compute_drift(OLD_DDL, OLD_DDL))
    assert "No blocking findings" in report


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------
def _seeded_engine(rows: int) -> ExecutionEngine:
    backend = SQLiteBackend(None)
    connection = backend.connection
    connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL, status TEXT)")
    connection.executemany(
        "INSERT INTO orders VALUES (?, ?, ?)",
        [(i, i * 1.5, "paid" if i % 2 else "pending") for i in range(1, rows + 1)],
    )
    connection.commit()
    return ExecutionEngine(backend)


def test_benchmarks_return_plans_and_latency() -> None:
    baseline = _seeded_engine(40)
    candidate = _seeded_engine(40)
    try:
        results, regressions = run_benchmarks(
            baseline,
            candidate,
            [("status mix", "SELECT status, COUNT(*) FROM orders GROUP BY status")],
        )
        assert results[0].error is None
        assert results[0].baseline_ms is not None
        assert results[0].candidate_ms is not None
        assert isinstance(regressions, list)
    finally:
        baseline.close()
        candidate.close()


def test_benchmark_reports_broken_query() -> None:
    baseline = _seeded_engine(10)
    candidate = _seeded_engine(10)
    try:
        results, regressions = run_benchmarks(
            baseline, candidate, [("bad", "SELECT * FROM missing_table")]
        )
        assert results[0].error is not None
        assert any(regression.kind == "broken_query" for regression in regressions)
    finally:
        baseline.close()
        candidate.close()


def test_parse_benchmark_file() -> None:
    text = "-- label: revenue\nSELECT SUM(amount) FROM orders\n\n" "SELECT COUNT(*) FROM orders\n"
    queries = parse_benchmark_file(text)
    assert queries[0][0] == "revenue"
    assert queries[1][0] == "query 2"


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------
@pytest.fixture
def client(mocker: Any) -> GitHubClient:
    return GitHubClient(token="tok", repo="acme/data", session=mocker.Mock())


def test_client_validates_arguments() -> None:
    with pytest.raises(GitHubAPIError):
        GitHubClient(token="", repo="acme/data")
    with pytest.raises(GitHubAPIError):
        GitHubClient(token="t", repo="nope")


def test_list_pr_files(client: GitHubClient) -> None:
    client._session.request.return_value = _response(
        200, [{"filename": "db/m.sql", "status": "modified", "additions": 2, "deletions": 1}]
    )
    assert client.list_pr_files(7) == [
        PrFile(filename="db/m.sql", status="modified", additions=2, deletions=1)
    ]
    call = client._session.request.call_args
    assert call.args[0] == "GET" and call.args[1].endswith("/pulls/7/files")
    assert call.kwargs["params"]["per_page"] == "100"


def test_api_errors_surface_status(client: GitHubClient) -> None:
    client._session.request.return_value = _response(403, {"message": "rate limited"})
    with pytest.raises(GitHubAPIError) as excinfo:
        client.list_pr_files(1)
    assert excinfo.value.status_code == 403
    assert "rate limited" in str(excinfo.value)


def test_new_file_has_empty_baseline(client: GitHubClient) -> None:
    client._session.request.return_value = _response(404, {"message": "Not Found"})
    assert client.get_file_content("db/new.sql", "main") == ""


def test_sticky_comment_create_then_update(client: GitHubClient) -> None:
    client._session.request.side_effect = [_response(200, []), _response(201, {"id": 1})]
    assert client.upsert_sticky_comment(9, "body " + COMMENT_MARKER) == "created"

    client._session.request.reset_mock()
    client._session.request.side_effect = [
        _response(200, [{"id": 1, "body": f"old {COMMENT_MARKER}"}]),
        _response(200, {"id": 1}),
    ]
    assert client.upsert_sticky_comment(9, "new " + COMMENT_MARKER) == "updated"
    patch = client._session.request.call_args
    assert patch.args[0] == "PATCH" and "/issues/comments/1" in patch.args[1]
