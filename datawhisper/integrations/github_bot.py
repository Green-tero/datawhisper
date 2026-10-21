"""GitHub REST integration: schema drift, query benchmarks and sticky PR reports.

The bot implements the "auditor" persona of DataWhisper. On every relevant
pull request it:

1. Fetches changed ``.sql`` files at the base and head revisions.
2. Parses ``CREATE TABLE`` statements with ``sqlglot`` and computes column
   level drift (added / removed / type-changed, plus whole-table changes).
3. Optionally benchmarks a fixed query suite against baseline and candidate
   databases.
4. Upserts a *sticky* comment (found via an HTML anchor marker) so repeated
   pushes update one comment instead of flooding the conversation.

Rendering note: GitHub strips ``<script>`` from comments, so interactive
Plotly charts cannot be embedded inline. The comment therefore carries
unicode bar visualizations plus a link to the interactive HTML artifact
uploaded by the workflow; the full dashboard remains available offline.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import requests
import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

from datawhisper.core.database import ExecutionEngine

#: Invisible anchor that makes the audit comment findable and updatable.
COMMENT_MARKER = "<!-- datawhisper:audit -->"

_GITHUB_API_VERSION = "2022-11-28"


class GitHubAPIError(RuntimeError):
    """Raised when the GitHub REST API returns a non-success status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"GitHub API error {status_code}: {message}")
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Schema drift detection
# ---------------------------------------------------------------------------
class ColumnChange(BaseModel):
    column: str
    old_type: str
    new_type: str


class TableDrift(BaseModel):
    table: str
    source: str = ""
    added_columns: list[str] = Field(default_factory=list)
    removed_columns: list[str] = Field(default_factory=list)
    type_changes: list[ColumnChange] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added_columns or self.removed_columns or self.type_changes)


class DriftReport(BaseModel):
    tables: list[TableDrift] = Field(default_factory=list)
    added_tables: list[str] = Field(default_factory=list)
    dropped_tables: list[str] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.tables or self.added_tables or self.dropped_tables)


def _extract_tables(sql_text: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Parse CREATE TABLE DDL into ``{table: {column: normalized_type}}``.

    Statements that fail to parse (dialect-specific tooling, procedural
    blocks) are collected instead of crashing the audit; the report surfaces
    them so a human can review what the automated diff skipped.
    """
    tables: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    try:
        statements = [s for s in sqlglot.parse(sql_text) if s is not None]
    except sqlglot.errors.ParseError as exc:
        return {}, [f"migration file unparsable: {exc}"]
    for statement in statements:
        if not (isinstance(statement, exp.Create) and statement.kind == "TABLE"):
            continue
        table_node = statement.find(exp.Table)
        schema = statement.find(exp.Schema)
        if table_node is None or schema is None:
            continue
        columns: dict[str, str] = {}
        for column_def in schema.find_all(exp.ColumnDef):
            kind = column_def.args.get("kind")
            columns[column_def.name.lower()] = kind.sql().upper() if kind is not None else "UNKNOWN"
        tables[table_node.name.lower()] = columns
    return tables, errors


def compute_drift(old_sql: str, new_sql: str, source: str = "") -> DriftReport:
    """Diff two migration snapshots at the column level."""
    old_tables, old_errors = _extract_tables(old_sql)
    new_tables, new_errors = _extract_tables(new_sql)
    report = DriftReport(parse_errors=old_errors + new_errors)

    for name in sorted(set(old_tables) - set(new_tables)):
        report.dropped_tables.append(name)
    for name in sorted(set(new_tables) - set(old_tables)):
        report.added_tables.append(name)

    for name in sorted(set(old_tables) & set(new_tables)):
        old_cols, new_cols = old_tables[name], new_tables[name]
        drift = TableDrift(table=name, source=source)
        drift.added_columns = sorted(set(new_cols) - set(old_cols))
        drift.removed_columns = sorted(set(old_cols) - set(new_cols))
        for col in sorted(set(old_cols) & set(new_cols)):
            if old_cols[col] != new_cols[col]:
                drift.type_changes.append(
                    ColumnChange(column=col, old_type=old_cols[col], new_type=new_cols[col])
                )
        if drift.has_changes:
            report.tables.append(drift)
    return report


def merge_drifts(reports: Sequence[DriftReport]) -> DriftReport:
    """Combine per-file drift reports into one PR-level report."""
    merged = DriftReport()
    seen: set[str] = set()
    for report in reports:
        for table in report.tables:
            if table.table in seen:
                continue  # first occurrence wins; migrations are ordered anyway
            seen.add(table.table)
            merged.tables.append(table)
        merged.added_tables.extend(t for t in report.added_tables if t not in merged.added_tables)
        merged.dropped_tables.extend(
            t for t in report.dropped_tables if t not in merged.dropped_tables
        )
        merged.parse_errors.extend(report.parse_errors)
    return merged


# ---------------------------------------------------------------------------
# Query benchmarks
# ---------------------------------------------------------------------------
class BenchmarkResult(BaseModel):
    label: str
    sql: str
    baseline_ms: float | None = None
    candidate_ms: float | None = None
    delta_ms: float | None = None
    delta_pct: float | None = None
    regression: bool = False
    error: str | None = None


def parse_benchmark_file(text: str) -> list[tuple[str, str]]:
    """Parse a benchmark suite file: blank-line separated blocks, ``-- label: x`` headers."""
    queries: list[tuple[str, str]] = []
    label: str | None = None
    lines: list[str] = []

    def flush() -> None:
        nonlocal label, lines
        if lines:
            sql = "\n".join(lines).strip().rstrip(";")
            if sql:
                queries.append((label or f"query {len(queries) + 1}", sql))
        label, lines = None, []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("-- label:"):
            flush()
            label = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("--"):
            continue
        else:
            lines.append(raw_line)
    flush()
    return queries


def _best_of(engine: ExecutionEngine, sql: str, repeats: int) -> float:
    """Minimum elapsed time across repeats (dampens scheduler noise)."""
    timings = [engine.execute(sql, validate=True).elapsed_ms for _ in range(max(1, repeats))]
    return min(timings)


def run_benchmarks(
    baseline: ExecutionEngine,
    candidate: ExecutionEngine,
    queries: Sequence[tuple[str, str]],
    *,
    repeats: int = 3,
    regression_factor: float = 1.25,
    min_delta_ms: float = 5.0,
) -> list[BenchmarkResult]:
    """Benchmark identical SQL on two snapshots and flag performance regressions."""
    results: list[BenchmarkResult] = []
    for label, sql in queries:
        try:
            baseline_ms = _best_of(baseline, sql, repeats)
            candidate_ms = _best_of(candidate, sql, repeats)
        except Exception as exc:
            results.append(BenchmarkResult(label=label, sql=sql, error=str(exc)))
            continue
        delta = candidate_ms - baseline_ms
        delta_pct = (delta / baseline_ms * 100.0) if baseline_ms > 0 else None
        regression = delta > min_delta_ms and (
            baseline_ms <= 0 or candidate_ms > baseline_ms * regression_factor
        )
        results.append(
            BenchmarkResult(
                label=label,
                sql=sql,
                baseline_ms=round(baseline_ms, 3),
                candidate_ms=round(candidate_ms, 3),
                delta_ms=round(delta, 3),
                delta_pct=round(delta_pct, 1) if delta_pct is not None else None,
                regression=regression,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------
def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a GitHub-flavored markdown table, escaping pipes in cells."""

    def cell(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(cell(h) for h in headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    lines.extend("| " + " | ".join(cell(c) for c in row) + " |" for row in rows)
    return "\n".join(lines)


def unicode_bar(value: float, maximum: float, width: int = 10) -> str:
    """Inline latency bar; the only chart form GitHub markdown renders reliably."""
    if maximum <= 0:
        return "`" * 0 + "░" * width
    filled = round(width * max(0.0, min(value / maximum, 1.0)))
    return "█" * filled + "░" * (width - filled)


def format_audit_report(
    *,
    drift: DriftReport | None = None,
    benchmarks: Sequence[BenchmarkResult] | None = None,
    insights: str | None = None,
    pr_number: int | None = None,
    base_ref: str = "",
    head_ref: str = "",
    artifact_url: str | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Compose the full PR comment, anchored by the sticky marker."""
    now = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [COMMENT_MARKER, "## 📊 DataWhisper Audit Report", ""]

    context_bits = [f"**PR #{pr_number}**" if pr_number else "**Local audit**"]
    if base_ref and head_ref:
        context_bits.append(f"`{base_ref}…{head_ref}`")
    context_bits.append(f"generated {now}")
    lines.append(" • ".join(context_bits))
    lines.append("")

    # --- Schema drift -------------------------------------------------
    lines.append("### 🧬 Schema Drift")
    if drift is None or not drift.has_changes:
        lines.append("✅ No schema drift detected in `CREATE TABLE` statements.")
    else:
        if drift.added_tables:
            lines.append("🆕 **New tables:** " + ", ".join(f"`{t}`" for t in drift.added_tables))
        if drift.dropped_tables:
            lines.append(
                "🗑️ **Dropped tables:** " + ", ".join(f"`{t}`" for t in drift.dropped_tables)
            )
        for table in drift.tables:
            lines.append("")
            origin = f" (`{table.source}`)" if table.source else ""
            lines.append(f"#### `{table.table}`{origin}")
            rows: list[list[str]] = []
            rows.extend(["+ added", f"`{c}`", ""] for c in table.added_columns)
            rows.extend(["- removed", f"`{c}`", ""] for c in table.removed_columns)
            rows.extend(
                ["~ changed", f"`{c.column}`", f"`{c.old_type}` -> `{c.new_type}`"]
                for c in table.type_changes
            )
            lines.append(markdown_table(["Change", "Column", "Details"], rows))
        if drift.parse_errors:
            lines.append("")
            lines.append(
                f"⚠️ {len(drift.parse_errors)} statement(s) could not be parsed and "
                "were excluded from the automated diff."
            )
    lines.append("")

    # --- Benchmarks ----------------------------------------------------
    lines.append("### ⏱️ Query Benchmarks")
    if not benchmarks:
        lines.append("_No benchmark suite configured for this audit._")
    else:
        worst = max((b.candidate_ms for b in benchmarks if b.candidate_ms is not None), default=0.0)
        bench_rows: list[list[str]] = []
        for bench in benchmarks:
            if bench.error is not None:
                bench_rows.append([f"`{bench.label}`", "—", "—", f"❌ {bench.error}"])
                continue
            assert (
                bench.baseline_ms is not None
                and bench.candidate_ms is not None
                and bench.delta_ms is not None
            )
            delta_txt = f"{bench.delta_ms:+.1f} ms"
            if bench.delta_pct is not None:
                delta_txt += f" ({bench.delta_pct:+.1f}%)"
            flag = "⚠️ regression" if bench.regression else "✅"
            bench_rows.append(
                [
                    f"`{bench.label}`",
                    f"{bench.baseline_ms:.1f} ms",
                    f"{bench.candidate_ms:.1f} ms ` {unicode_bar(bench.candidate_ms, worst)} `",
                    f"{delta_txt} {flag}",
                ]
            )
        lines.append(markdown_table(["Query", "Baseline", "Candidate", "Δ / verdict"], bench_rows))
    lines.append("")

    # --- Agentic insights ----------------------------------------------
    if insights:
        lines.append("### 🤖 Agentic Insights")
        lines.append("<details><summary>Model-generated analysis (verify before merging)</summary>")
        lines.append("")
        lines.append(insights.strip())
        lines.append("")
        lines.append("</details>")
        lines.append("")

    if artifact_url:
        lines.append(f"📈 [Interactive Plotly report]({artifact_url})")
        lines.append("")
    lines.append(
        "---\n<sub>Generated by "
        "[DataWhisper](https://github.com/datawhisper/datawhisper) • "
        "read-only sandboxed analysis • all SQL validated at AST level</sub>"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------
class PrFile(BaseModel):
    filename: str
    status: str
    additions: int = 0
    deletions: int = 0


class GitHubClient:
    """Thin, dependency-light REST client for PR auditing."""

    def __init__(
        self,
        token: str,
        repo: str,
        api_base: str = "https://api.github.com",
        session: requests.Session | None = None,
        request_timeout: float = 20.0,
    ) -> None:
        if not token:
            raise GitHubAPIError(401, "a GitHub token is required")
        if "/" not in repo:
            raise GitHubAPIError(400, "repo must look like 'owner/name'")
        self._repo = repo
        self._api_base = api_base.rstrip("/")
        self._session = session or requests.Session()
        self._timeout = request_timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        response = self._session.request(
            method,
            url,
            headers={**self._headers, **(headers or {})},
            json=json_body,
            params=params,
            timeout=self._timeout,
        )
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = (
                    payload.get("message", response.text)
                    if isinstance(payload, dict)
                    else response.text
                )
            except ValueError:
                message = response.text
            raise GitHubAPIError(response.status_code, str(message))
        return response

    # --- Reads ---------------------------------------------------------
    def list_pr_files(self, pr_number: int) -> list[PrFile]:
        response = self._request(
            "GET",
            f"{self._api_base}/repos/{self._repo}/pulls/{pr_number}/files",
            params={"per_page": "100"},
        )
        payload = response.json()
        return [
            PrFile(
                filename=item["filename"],
                status=item.get("status", "modified"),
                additions=item.get("additions", 0),
                deletions=item.get("deletions", 0),
            )
            for item in payload
        ]

    def get_file_content(self, path: str, ref: str) -> str:
        """Fetch raw file content at a ref; absent files yield an empty baseline.

        A 404 is expected and meaningful: files newly added by the PR have no
        base revision, so the drift baseline is empty rather than an error.
        """
        try:
            response = self._request(
                "GET",
                f"{self._api_base}/repos/{self._repo}/contents/{path}",
                params={"ref": ref},
                headers={"Accept": "application/vnd.github.raw"},
            )
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return ""
            raise
        return response.text

    def list_pr_comments(self, pr_number: int) -> list[dict[str, Any]]:
        response = self._request(
            "GET", f"{self._api_base}/repos/{self._repo}/issues/{pr_number}/comments"
        )
        payload = response.json()
        return payload if isinstance(payload, list) else []

    # --- Writes --------------------------------------------------------
    def create_pr_comment(self, pr_number: int, body: str) -> None:
        self._request(
            "POST",
            f"{self._api_base}/repos/{self._repo}/issues/{pr_number}/comments",
            json_body={"body": body},
        )

    def update_pr_comment(self, comment_id: int, body: str) -> None:
        self._request(
            "PATCH",
            f"{self._api_base}/repos/{self._repo}/issues/comments/{comment_id}",
            json_body={"body": body},
        )

    def upsert_sticky_comment(self, pr_number: int, body: str, marker: str = COMMENT_MARKER) -> str:
        """Update the existing audit comment or create it; returns the outcome.

        Stickiness keeps the PR conversation clean: every push to the PR
        revises the same comment (anchored by ``marker``) instead of adding
        a new one.
        """
        for comment in self.list_pr_comments(pr_number):
            if marker in str(comment.get("body", "")):
                comment_id = int(comment["id"])
                self.update_pr_comment(comment_id, body)
                return "updated"
        self.create_pr_comment(pr_number, body)
        return "created"
