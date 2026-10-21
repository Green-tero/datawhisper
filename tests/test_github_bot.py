"""GitHub bot: drift computation, report formatting and REST payload behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from datawhisper.core.database import ExecutionEngine, SQLiteBackend
from datawhisper.integrations.github_bot import (
    COMMENT_MARKER,
    GitHubAPIError,
    GitHubClient,
    PrFile,
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
CREATE TABLE audit_events (
  id INTEGER PRIMARY KEY,
  kind TEXT
);
"""

NEW_DDL = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL,
  age BIGINT,
  is_active BOOLEAN,
  signup_at TIMESTAMP
);
"""


def _response(status: int, payload: Any = None, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(status_code=status, json=lambda: payload, text=text)


# ---------------------------------------------------------------------------
# Schema drift
# ---------------------------------------------------------------------------
def test_column_level_drift_detection() -> None:
    report = compute_drift(OLD_DDL, NEW_DDL, source="migrations/001.sql")
    users = next(t for t in report.tables if t.table == "users")
    assert "is_active" in users.added_columns
    assert "signup_at" in users.added_columns
    assert "legacy_flag" in users.removed_columns
    age_change = next(c for c in users.type_changes if c.column == "age")
    assert age_change.old_type.upper() in {"INTEGER", "INT"}
    assert age_change.new_type.upper() == "BIGINT"
    assert report.dropped_tables == ["audit_events"]
    assert report.has_changes


def test_no_drift_when_identical() -> None:
    report = compute_drift(OLD_DDL, OLD_DDL)
    assert not report.has_changes
    assert report.tables == []


def test_unparsable_migration_is_reported_not_fatal() -> None:
    report = compute_drift("CREATE TABLE (broken syntax;", NEW_DDL)
    assert report.parse_errors


def test_merge_drifts_deduplicates_tables() -> None:
    first = compute_drift(OLD_DDL, NEW_DDL, source="a.sql")
    second = compute_drift(OLD_DDL, NEW_DDL, source="b.sql")
    merged = merge_drifts([first, second])
    assert len(merged.tables) == 1
    assert merged.tables[0].source == "a.sql"


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------
def test_markdown_table_escapes_pipes() -> None:
    table = markdown_table(["h1", "h2"], [["a|b", "c"]])
    assert table.splitlines()[0] == "| h1 | h2 |"
    assert "a\\|b" in table


def test_unicode_bar_scaling() -> None:
    assert unicode_bar(10, 10, width=4) == "████"
    assert unicode_bar(0, 10, width=4) == "░░░░"
    assert unicode_bar(5, 10, width=4) == "██░░"


def test_audit_report_contains_all_sections() -> None:
    drift = compute_drift(OLD_DDL, NEW_DDL)
    markdown = format_audit_report(
        drift=drift,
        benchmarks=[],
        insights="Null ratio on `users.age` jumped to 31%.",
        pr_number=42,
        base_ref="main",
        head_ref="feature",
        artifact_url="https://example.com/report",
    )
    lines = markdown.splitlines()
    assert lines[0] == COMMENT_MARKER
    assert "PR #42" in markdown
    assert "`users`" in markdown
    assert "BIGINT" in markdown
    assert "<details>" in markdown
    assert "Null ratio" in markdown
    assert "https://example.com/report" in markdown
    assert "sandbox" in markdown


# ---------------------------------------------------------------------------
# REST client (mocked session)
# ---------------------------------------------------------------------------
@pytest.fixture
def client(mocker: Any) -> GitHubClient:
    session = mocker.Mock()
    return GitHubClient(token="tok", repo="acme/data", session=session)


def test_client_requires_token_and_repo_shape() -> None:
    with pytest.raises(GitHubAPIError):
        GitHubClient(token="", repo="acme/data")
    with pytest.raises(GitHubAPIError):
        GitHubClient(token="t", repo="not-a-repo")


def test_list_pr_files_parses_payload(client: GitHubClient, mocker: Any) -> None:
    client._session.request.return_value = _response(
        200,
        [{"filename": "db/migrate.sql", "status": "modified", "additions": 3, "deletions": 1}],
    )
    files = client.list_pr_files(7)
    assert files == [PrFile(filename="db/migrate.sql", status="modified", additions=3, deletions=1)]
    call = client._session.request.call_args
    assert call.args[0] == "GET"
    assert call.args[1].endswith("/repos/acme/data/pulls/7/files")
    assert call.kwargs["headers"]["Authorization"] == "Bearer tok"
    assert call.kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"


def test_api_error_surfaces_status(client: GitHubClient) -> None:
    client._session.request.return_value = _response(403, {"message": "rate limited"})
    with pytest.raises(GitHubAPIError) as excinfo:
        client.list_pr_files(1)
    assert excinfo.value.status_code == 403
    assert "rate limited" in str(excinfo.value)


def test_sticky_comment_created_then_updated(client: GitHubClient) -> None:
    # First audit run: no prior comment, expect POST.
    client._session.request.side_effect = [
        _response(200, []),
        _response(201, {"id": 100}),
    ]
    assert client.upsert_sticky_comment(9, "body A " + COMMENT_MARKER) == "created"
    post_call = client._session.request.call_args
    assert post_call.args[0] == "POST"
    assert COMMENT_MARKER in post_call.kwargs["json"]["body"]

    # Second audit run: marker found in an existing comment, expect PATCH.
    client._session.request.reset_mock()
    client._session.request.side_effect = [
        _response(200, [{"id": 100, "body": f"stale {COMMENT_MARKER}"}]),
        _response(200, {"id": 100}),
    ]
    assert client.upsert_sticky_comment(9, "body B " + COMMENT_MARKER) == "updated"
    patch_call = client._session.request.call_args
    assert patch_call.args[0] == "PATCH"
    assert "/issues/comments/100" in patch_call.args[1]


def test_get_file_content_uses_raw_accept(client: GitHubClient) -> None:
    client._session.request.return_value = _response(200, text="CREATE TABLE t (x INTEGER);")
    content = client.get_file_content("db/migrate.sql", "abc123")
    assert content.startswith("CREATE TABLE")
    call = client._session.request.call_args
    assert call.kwargs["headers"]["Accept"] == "application/vnd.github.raw"
    assert call.kwargs["params"] == {"ref": "abc123"}


# ---------------------------------------------------------------------------
# Benchmark harness
# ---------------------------------------------------------------------------
def _seeded_engine(rows: int) -> ExecutionEngine:
    backend = SQLiteBackend(None)
    connection = backend.connection
    connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL, status TEXT)")
    connection.executemany(
        "INSERT INTO orders VALUES (?, ?, ?)",
        [(i, float(i) * 1.5, "paid" if i % 2 else "pending") for i in range(1, rows + 1)],
    )
    connection.commit()
    return ExecutionEngine(backend)


def test_benchmark_suite_success_and_error_paths() -> None:
    baseline = _seeded_engine(50)
    candidate = _seeded_engine(50)
    try:
        results = run_benchmarks(
            baseline,
            candidate,
            [("orders by status", "SELECT status, COUNT(*) FROM orders GROUP BY status")],
        )
        ok = results[0]
        assert ok.error is None
        assert ok.baseline_ms is not None and ok.candidate_ms is not None
        assert ok.delta_ms == pytest.approx(ok.candidate_ms - ok.baseline_ms, abs=0.01)

        missing = run_benchmarks(
            baseline, candidate, [("bad table", "SELECT * FROM does_not_exist")]
        )[0]
        assert missing.error is not None
        assert missing.regression is False
    finally:
        baseline.close()
        candidate.close()


def test_parse_benchmark_file_labels_and_blocks() -> None:
    text = (
        "-- label: revenue by status\n"
        "SELECT status, SUM(amount) FROM orders GROUP BY status\n\n"
        "SELECT COUNT(*) FROM customers\n"
    )
    queries = parse_benchmark_file(text)
    assert queries[0][0] == "revenue by status"
    assert "SUM(amount)" in queries[0][1]
    assert queries[1][0] == "query 2"
